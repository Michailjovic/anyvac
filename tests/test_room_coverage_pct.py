"""Tests for the persistent per-room coverage % (docs/29, ratified 2026-07-29).

Unlike the live `_build_progress` gauge (reads `_room_cells`, which resets to
empty the instant a session ends), `_room_coverage` is a durable "last clean
covered X%" snapshot — mirrors `_history`'s room-NAME keying (shared across
the fleet, whichever pass most recently completed wins), computed once per
completed room/kind at session end in `_track_and_emit`, against the baseline
as it stood BEFORE that same session's own `_learn_coverage` update.

Harness copied from `test_coordinator_pipeline.py` (established per-file
duplication convention in this test suite, see that file's docstring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    def async_delay_save(self, get_data: Any, delay: float) -> None:
        return None


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def _new_coordinator(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeHass()
    for attr in (
        "_store", "_est_store", "_cov_store", "_cov_pct_store", "_sel_store",
        "_pins_store", "_seq_store", "_layers_store", "_paths_store",
    ):
        setattr(coord, attr, _FakeStore())
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
    coord._listeners = {}  # DataUpdateCoordinator internal, needed by reset_learning()'s
    # async_update_listeners() call — object.__new__ skips the real __init__ that sets this.
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock.now)
    return coord


# Hall: 0..1000mm both axes -> 5x5 = 25 possible 250mm cells (COVERAGE_CELL_MM),
# so a 5-cell baseline sits exactly AT the 20%-of-bbox poison-guard threshold
# (25 * 0.2 == 5) instead of tripping it — chosen deliberately so a second
# session reading the baseline back isn't itself rejected as implausible.
ROOMS = [{"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000}]


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "dry",
        "vacuum_room_name": None,
        "rooms": [dict(r) for r in ROOMS],
        "cleaned_rooms": [],
        "_path_dry": [],
        "_path_wet": [],
    }
    data.update(overrides)
    return AnyVacDevice(duid=duid, slug=duid, name=duid, data=data)


def _poll(coord: AnyVacCoordinator, device: AnyVacDevice) -> None:
    coord._update_history(device)
    coord._detect_room_done(device)
    coord._track_and_emit(device)
    coord._attribute_points(device)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


def _clean_hall(coord: AnyVacCoordinator, clock: _Clock, duid: str, points: list[dict[str, float]]) -> None:
    """Runs one full session cleaning Hall: first poll (unconfirmed), then one
    poll per subsequent point (confirmed from the 2nd poll onward, same debounce
    pattern as test_coordinator_pipeline.py), then docks to end the session."""
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=points[:1]))
    for i in range(2, len(points) + 1):
        clock.advance(minutes=5)
        _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=points[:i]))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))


def test_no_baseline_yet_leaves_coverage_unset(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """First-ever completed clean of a room must NOT persist a % (docs/29 §4.3:
    no baseline yet -> "—" on the card, never a naive/misleading number) even
    though the clean itself is accepted and creates the baseline for next time."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"
    _clean_hall(coord, clock, duid, [
        {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
    ])
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 3  # baseline WAS created...
    assert coord._room_coverage == {}  # ...but nothing persisted for display yet


def test_second_session_computes_pct_against_prior_baseline(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A second completed clean covering fewer cells than the learned baseline
    persists a real, non-trivial percentage (not just an echo of 100%) — proof
    it's an actual division, using the baseline as it stood BEFORE this
    session (docs/29 §4.1), not one already inflated by this session's cells."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"

    # Session 1: 5 distinct cells -> baseline = 5.
    _clean_hall(coord, clock, duid, [
        {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
        {"x": 100, "y": 400}, {"x": 100, "y": 700},
    ])
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5
    assert coord._room_coverage == {}

    # Session 2: only 4 of those 5 cells -> 80% of the prior baseline.
    clock.advance(minutes=5)
    _clean_hall(coord, clock, duid, [
        {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
        {"x": 100, "y": 400},
    ])
    assert coord._room_coverage["Hall"]["dry"] == 80


def test_reset_learning_clears_persisted_coverage(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """anyvac.reset_learning(baselines=True) must also drop the persisted %
    computed against the baseline it just wiped — otherwise the card would
    keep showing a number anchored to a baseline that no longer exists."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"
    _clean_hall(coord, clock, duid, [
        {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
        {"x": 100, "y": 400}, {"x": 100, "y": 700},
    ])
    clock.advance(minutes=5)
    _clean_hall(coord, clock, duid, [
        {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
        {"x": 100, "y": 400},
    ])
    assert coord._room_coverage["Hall"]["dry"] == 80

    coord.reset_learning(baselines=True)
    assert coord._cov_baseline.get(duid, {}) == {}  # `_prune` leaves an empty duid shell
    assert coord._room_coverage == {}
