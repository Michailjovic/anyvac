"""Tests for AnyVacCoordinator's per-kind room pins (docs/18 §7e, per-kind
since 2026-07-25 — see `tests/test_planner_pin.py` for the planner-side
half of this fix, and the CHANGELOG entry for the full field-report story).

Covers `set_room_pin`/`room_pins` directly, the persisted-pin migration in
`_async_setup` (a pre-upgrade flat `{room: vacuum}` entry carries no kind
info to migrate onto and must be dropped, not guessed), and the
room-finished auto-clear now only releasing the pass that actually just
ran instead of the whole room.

Like `test_coordinator_pipeline.py`, coordinators are built via
`object.__new__` to skip `__init__` — only the state each code path under
test actually touches is initialised.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import custom_components.anyvac.coordinator as coordinator_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice

UTC = timezone.utc


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()


class _FakeStore:
    """No-op stand-in for homeassistant.helpers.storage.Store, optionally
    returning a preset value from `async_load` for the migration tests."""

    def __init__(self, load_value: Any = None) -> None:
        self._load_value = load_value
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self._load_value

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        self.saved = get_data()


def _bare_coordinator() -> AnyVacCoordinator:
    """A coordinator with only `room_pins`/`set_room_pin`'s own state set up
    (no pipeline/poll machinery — see `_pin_lifecycle_coordinator` below for
    that). `_listeners` is DataUpdateCoordinator's own state, needed because
    `set_room_pin` calls `async_update_listeners()`."""
    coord = object.__new__(AnyVacCoordinator)
    coord._room_pins = {}
    coord._pins_store = _FakeStore()
    coord._listeners = {}
    return coord


# -- set_room_pin / room_pins -------------------------------------------------


def test_set_room_pin_stores_dry_and_wet_independently() -> None:
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    coord.set_room_pin("Bedroom", "vacuum.s8_maxv", "wet")
    assert coord.room_pins == {
        "Bedroom": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"}
    }


def test_set_room_pin_kind_overwrite_does_not_touch_other_kind() -> None:
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    coord.set_room_pin("Bedroom", "vacuum.s8_maxv", "wet")
    coord.set_room_pin("Bedroom", "vacuum.s7_maxv", "dry")  # re-pin dry only
    assert coord.room_pins == {
        "Bedroom": {"dry": "vacuum.s7_maxv", "wet": "vacuum.s8_maxv"}
    }


def test_set_room_pin_unpin_single_kind() -> None:
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    coord.set_room_pin("Bedroom", "vacuum.s8_maxv", "wet")
    coord.set_room_pin("Bedroom", None, "dry")  # unpin dry only
    assert coord.room_pins == {"Bedroom": {"wet": "vacuum.s8_maxv"}}


def test_set_room_pin_unpin_last_kind_drops_room_entirely() -> None:
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    coord.set_room_pin("Bedroom", None, "dry")
    assert coord.room_pins == {}


def test_set_room_pin_no_kind_unpins_both_passes() -> None:
    """`kind` omitted entirely = unpin the whole room — the shorthand the
    card's `_toggleRoomAcross` uses when a room is deselected."""
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    coord.set_room_pin("Bedroom", "vacuum.s8_maxv", "wet")
    coord.set_room_pin("Bedroom")
    assert coord.room_pins == {}


def test_room_pins_property_returns_a_copy() -> None:
    """Mutating the returned dict (outer or inner) must not corrupt the
    coordinator's own state — the property returns a fresh copy at both
    levels, not a view onto the live per-room dicts."""
    coord = _bare_coordinator()
    coord.set_room_pin("Bedroom", "vacuum.s6_kitchen", "dry")
    snapshot = coord.room_pins
    snapshot["Bedroom"]["dry"] = "vacuum.tampered"
    snapshot["Kitchen"] = {"dry": "vacuum.also_tampered"}
    assert coord.room_pins == {"Bedroom": {"dry": "vacuum.s6_kitchen"}}


# -- persisted-pin migration (_async_setup) -----------------------------------


async def _load_pins(load_value: Any) -> dict[str, dict[str, str]]:
    coord = object.__new__(AnyVacCoordinator)
    coord._store = _FakeStore()
    coord._sel_store = _FakeStore()
    coord._pins_store = _FakeStore(load_value)
    coord._seq_store = _FakeStore()
    coord._layers_store = _FakeStore()
    coord._cov_store = _FakeStore()
    coord._cov_pct_store = _FakeStore()
    coord._est_store = _FakeStore()
    coord._paths_store = _FakeStore()
    coord._room_pins = {}  # __init__'s default, in case `load_value` isn't a dict
    await coord._async_setup()
    return coord._room_pins


@pytest.mark.asyncio
async def test_migration_keeps_new_per_kind_format() -> None:
    pins = await _load_pins(
        {"Bedroom": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"}}
    )
    assert pins == {"Bedroom": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"}}


@pytest.mark.asyncio
async def test_migration_drops_legacy_flat_format() -> None:
    """A pre-2026-07-25 flat `{room: vacuum}` entry carries no kind info to
    migrate onto — dropped rather than guessed (equivalent to a one-time
    reset to automatic assignment for that room)."""
    pins = await _load_pins({"Bedroom": "vacuum.s6_kitchen"})
    assert pins == {}


@pytest.mark.asyncio
async def test_migration_filters_unknown_kind_keys_and_empty_values() -> None:
    pins = await _load_pins(
        {
            "Bedroom": {"dry": "vacuum.s6_kitchen", "bogus": "vacuum.x", "wet": ""},
            "EmptyRoom": {"bogus": "vacuum.x"},
        }
    )
    assert pins == {"Bedroom": {"dry": "vacuum.s6_kitchen"}}


@pytest.mark.asyncio
async def test_migration_handles_no_stored_pins() -> None:
    assert await _load_pins(None) == {}


# -- room-finished auto-clear, per pass ---------------------------------------


def _pin_lifecycle_coordinator(clock_now: datetime) -> AnyVacCoordinator:
    """Just enough state for a dry-session finish on room "Hall" to reach
    the pin auto-clear code — mirrors `test_coordinator_pipeline.py`'s
    `_new_coordinator`/`_poll` pattern, trimmed to what this path touches."""
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeHass()
    for attr in ("_store", "_est_store", "_cov_store", "_cov_pct_store", "_sel_store", "_seq_store", "_layers_store", "_paths_store"):
        setattr(coord, attr, _FakeStore())
    coord._pins_store = _FakeStore()
    coord._history = {}
    coord._was_cleaning = {}
    coord._session_rooms = {}
    coord._session_start = {}
    coord._estimates = {}
    coord._raw_room = {}
    coord._raw_count = {}
    coord._confirmed_room = {}
    coord._session_confirmed = {}
    coord._session_clean_type = {}
    coord._last_calib = {}
    coord._selected_rooms = set()
    coord._room_pins = {}
    coord._room_sequence = {}
    coord._room_elapsed = {}
    coord._last_poll = {}
    coord._room_cells = {}
    coord._job_rooms = {}
    coord._job_seq = 0
    coord._job_id = {}
    coord._path_job_id = {}
    coord._transit_cells = {}
    coord._path_seen = {}
    coord._cov_baseline = {}
    coord._room_coverage = {}
    coord._dry_path = {}
    coord._dry_path_open = {}
    coord._wet_path = {}
    coord._wet_path_open = {}
    coord._decim_cache = {}
    coord._known_duids = set()
    coord._pipeline_warned = False
    coord._view_layers = {"dry": True, "wet": False}
    coord._debug_seen = {}
    coord._expose_legacy_mm = False
    return coord


_ROOMS = [{"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000}]


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "dry",
        "vacuum_room_name": None,
        "rooms": [dict(r) for r in _ROOMS],
        "cleaned_rooms": [],
        "_path_dry": [],
        "_path_wet": [],
    }
    data.update(overrides)
    return AnyVacDevice(duid=duid, slug=duid, name=duid, data=data)


def _run_dry_session_on_hall(coord: AnyVacCoordinator, duid: str) -> None:
    """Start, cleaning-poll, then dock a dry session on room "Hall" — the
    minimum needed to reach `_clean_finished`'s pin auto-clear with a known
    `ct == "dry"`. Deliberately not chasing the calibration coverage floor
    (irrelevant here: the pin-clear reads `_session_rooms`, not calibration
    acceptance)."""
    coord._update_history(_device(duid, vacuum_room_name="Hall"))
    coord._detect_room_done(_device(duid, vacuum_room_name="Hall"))
    coord._track_and_emit(_device(duid, vacuum_room_name="Hall"))
    coord._attribute_points(_device(duid, vacuum_room_name="Hall"))
    finishing = _device(duid, in_cleaning=False)
    coord._update_history(finishing)
    coord._detect_room_done(finishing)
    coord._track_and_emit(finishing)
    coord._attribute_points(finishing)


def test_finish_clears_only_the_pass_that_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """A room pinned for BOTH passes to different vacuums: finishing the dry
    robot's session must clear only the dry pin, leaving the still-pending
    wet pin untouched — the bug this whole fix targets (a `both` job's dry
    robot finishing first used to wipe the wet pin before the wet robot had
    even started)."""
    clock = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock)
    coord = _pin_lifecycle_coordinator(clock)
    coord._room_pins = {"Hall": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"}}

    _run_dry_session_on_hall(coord, "d1")

    assert coord._room_pins == {"Hall": {"wet": "vacuum.s8_maxv"}}


def test_finish_drops_room_entirely_once_last_pass_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock)
    coord = _pin_lifecycle_coordinator(clock)
    coord._room_pins = {"Hall": {"dry": "vacuum.s6_kitchen"}}  # only a dry pin exists

    _run_dry_session_on_hall(coord, "d1")

    assert coord._room_pins == {}
