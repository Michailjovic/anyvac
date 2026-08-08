"""Tests for dry/wet trace persistence across a HA restart (2026-07-26 follow-up
to docs/27).

Field report: after restarting HA, a vacuum's dry trace showed nothing at all
(its gate only appends while the robot is actively vacuuming, which an idle
post-restart robot never is again until its next clean) while the wet trace
showed one giant undifferentiated blob (no such gate — the very first poll
after restart re-diffs the robot's raw mop path against a freshly-empty
`_path_seen`, so the ENTIRE last-session raw buffer gets dumped as "new").
User's actual ask, once clarified: both traces should survive a restart and
keep showing the last clean exactly as it looked before — reset only on a
genuinely new job/sortie (`_sortie_is_new_job`, see `test_coordinator_pipeline.
py::test_path_wipes_on_a_genuinely_new_job`), never on the restart itself.

Fix: `_dry_path`/`_wet_path` + the bookkeeping needed to resume diffing
correctly (`_path_seen`, `_dry_path_open`/`_wet_path_open`) are now persisted
through a dedicated `Store` (`_paths_store`), loaded in `_async_setup` and
saved (debounced) whenever they actually change.

These tests simulate a restart literally: run a poll sequence against one
coordinator instance ("before"), capture what got saved to the fake backing
store, then build a BRAND NEW coordinator instance ("after") that loads from
that same store — exactly what `_async_setup` would do against a real HA
`Store` after a process restart — and assert the reloaded state matches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import custom_components.anyvac.coordinator as coordinator_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice

UTC = timezone.utc
pytestmark = pytest.mark.asyncio


class _FakeBus:
    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        pass


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()


class _FakeStore:
    """Unlike `test_coordinator_pipeline.py`'s no-op version, this one actually
    round-trips: `async_load` returns whatever was last (synchronously, no real
    delay) saved via `async_delay_save` — or a preset `load_value`, letting a
    second coordinator instance be seeded with the first's saved snapshot to
    simulate a restart."""

    def __init__(self, load_value: Any = None) -> None:
        self._load_value = load_value
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self.saved if self.saved is not None else self._load_value

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        self.saved = get_data()


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
]


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "both",
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


def _new_coordinator(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, paths_store: _FakeStore | None = None
) -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeHass()
    for attr in (
        "_store", "_est_store", "_cov_store", "_cov_pct_store", "_sel_store",
        "_pins_store", "_seq_store", "_layers_store",
    ):
        setattr(coord, attr, _FakeStore())
    coord._paths_store = paths_store or _FakeStore()
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
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock.now)
    return coord


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


async def test_finished_clean_survives_a_simulated_restart(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The core field ask: a completed clean's dry AND wet trace must look
    identical after a restart, not blank (dry) / an undifferentiated blob
    (wet)."""
    store = _FakeStore()
    before = _new_coordinator(monkeypatch, clock, paths_store=store)
    duid = "d1"

    _poll(before, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
        _path_wet=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(before, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
        _path_wet=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
    ))
    clock.advance(minutes=5)
    # Docking poll: the robot's OWN raw path buffer does NOT reset to empty the
    # instant it docks (it persists until the robot's NEXT sortie actually
    # starts moving again — this is exactly why a naive post-restart re-diff
    # dumps the whole last session as one blob, see the module docstring), so
    # this poll must keep reporting the same raw arrays as the previous one,
    # not reset them — resetting them here would itself look like a sortie
    # boundary and (correctly, for a no-job-scope session) wipe the trace this
    # test is trying to check survived, which is a fixture-realism concern,
    # not something this fix needs to special-case.
    _poll(before, _device(
        duid, in_cleaning=False,
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
        _path_wet=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
    ))

    assert before._dry_path[duid] == [
        [{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}]
    ]
    assert before._wet_path[duid] == [
        [{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}]
    ]
    assert store.saved is not None  # something actually got persisted

    # Simulate a restart: a BRAND NEW coordinator instance, seeded only from
    # what the (fake) Store holds on disk — exactly `_async_setup`'s contract.
    after = _new_coordinator(monkeypatch, clock, paths_store=store)
    await after._async_setup()

    # Since 1.1.0 the store holds an RDP-SIMPLIFIED copy (the raw trace was
    # rewritten in full on every poll — ~66 MiB over a long job, see
    # `_paths_for_save`), so the restored trace is shape-equivalent rather than
    # byte-identical: here the midpoint (400,100) sits exactly on the line
    # between (100,100) and (700,100), so it carries no information and is
    # dropped. Segment COUNT and endpoints are what actually matter for the
    # rendered polyline, and those must survive exactly.
    for layer, restored, original in (
        ("dry", after._dry_path[duid], before._dry_path[duid]),
        ("wet", after._wet_path[duid], before._wet_path[duid]),
    ):
        assert len(restored) == len(original), f"{layer}: segment count changed"
        for seg_after, seg_before in zip(restored, original):
            assert seg_after[0] == seg_before[0], f"{layer}: start point moved"
            assert seg_after[-1] == seg_before[-1], f"{layer}: end point moved"
            assert len(seg_after) <= len(seg_before), f"{layer}: simplification grew the path"
        assert restored == [[{"x": 100, "y": 100}, {"x": 700, "y": 100}]]

    # The diff cursor must NOT be simplified along with the geometry — it counts
    # RAW points consumed from the robot's buffer, so rewriting it would make the
    # next poll re-attribute points that were already counted.
    assert after._path_seen[duid] == before._path_seen[duid] == {"dry": 3, "wet": 3}
    assert after._dry_path_open.get(duid, False) == before._dry_path_open.get(duid, False)
    assert after._wet_path_open.get(duid, False) == before._wet_path_open.get(duid, False)


async def test_a_new_job_wipe_persists_as_empty_not_the_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The other half of the fix must not regress the job-id wipe from
    `test_coordinator_pipeline.py::test_path_wipes_on_a_genuinely_new_job`: if
    a genuinely new job wipes the trace, that wipe itself must be persisted
    immediately (not just implicitly overwritten next time new points land) —
    otherwise a restart landing in the small window right after a wipe but
    before the next poll's points would resurrect the OLD job's trace from a
    stale disk snapshot instead of showing the (correctly) empty new job."""
    store = _FakeStore()
    coord = _new_coordinator(monkeypatch, clock, paths_store=store)
    duid = "d2"

    coord.set_job_rooms(duid, {"Hall"})
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}], _path_wet=[{"x": 100, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    coord.set_job_rooms(duid, None)
    assert store.saved[duid]["dry_path"] == [[{"x": 100, "y": 100}]]

    # Brand new job for the same vacuum — its very first sortie-start
    # transition (in `_track_and_emit`) must wipe AND persist the wipe, even
    # before `_attribute_points` (same poll) has any new points to report.
    clock.advance(minutes=10)
    coord.set_job_rooms(duid, {"Hall"})
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=[], _path_wet=[]))

    assert coord._dry_path[duid] == []
    assert coord._wet_path[duid] == []
    assert store.saved[duid]["dry_path"] == []
    assert store.saved[duid]["wet_path"] == []


async def test_paths_for_save_round_trips_through_async_setup(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Direct unit check of the (de)serialisation shape, independent of the
    poll pipeline — segments, per-layer seen counts and open-flags all
    survive a `_paths_for_save` -> `_async_setup` round trip."""
    coord = _new_coordinator(monkeypatch, clock)
    coord._dry_path = {"d3": [[{"x": 1.5, "y": 2.5}], [{"x": 9.0, "y": 9.0}]]}
    coord._wet_path = {"d3": [[{"x": 1.5, "y": 2.5}]]}
    coord._path_seen = {"d3": {"dry": 3, "wet": 1}}
    coord._dry_path_open = {"d3": True}
    coord._wet_path_open = {"d3": False}
    snapshot = coord._paths_for_save()

    reloaded = _new_coordinator(monkeypatch, clock, paths_store=_FakeStore(load_value=snapshot))
    await reloaded._async_setup()

    assert reloaded._dry_path == coord._dry_path
    assert reloaded._wet_path == coord._wet_path
    assert reloaded._path_seen == coord._path_seen
    assert reloaded._dry_path_open == {"d3": True}
    assert reloaded._wet_path_open == {}  # False is the default, not stored as an override
