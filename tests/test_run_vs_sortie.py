"""Tests for RUN vs SORTIE accumulation (docs/36, field bug 2026-09-02).

A job dispatched progressively (docs/23) docks between its batches, and a
firmware path reset can happen mid-clean when the robot comes back for another
pass through a room. Both look like a fresh ``in_cleaning`` edge / a fresh
trajectory, but neither is a new CLEAN — and everything that measures the clean
(coverage cells, per-room active time, the run's start time, the learned
"full clean" baseline, the persisted coverage %) has to span the whole run.

Before docs/36 each sortie was harvested as if it were a complete clean, so a
room cleaned across a dock trip persisted a partial % and its ~60 %-of-a-clean
cell count was fed to `_learn_coverage`, dragging the baseline down towards the
size of one batch until every room eventually read 100 %.

Harness copied from `test_room_coverage_pct.py` (established per-file
duplication convention in this test suite).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice

UTC = timezone.utc


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, dict(data)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


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
    coord._run_pending = {}
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
    coord._listeners = {}
    monkeypatch.setattr("custom_components.anyvac.coordinator.dt_util.utcnow", lambda: clock.now)
    return coord


# Hall spans 0..1000 mm on both axes -> 5x5 = 25 cells of COVERAGE_CELL_MM, so a
# 5-cell baseline sits exactly AT the 20 %-of-bbox poison guard instead of
# tripping it (same geometry as test_room_coverage_pct.py). Kitchen is the
# neighbouring room the robot only ever drives through.
ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    {"segment_id": 2, "name": "Kitchen", "x0": 1000, "y0": 0, "x1": 2000, "y1": 1000},
]

# Five points, each in its own 250 mm cell of Hall.
HALL5 = [
    {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
    {"x": 100, "y": 400}, {"x": 100, "y": 700},
]


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


def _sortie(
    coord: AnyVacCoordinator, clock: _Clock, duid: str, points: list[dict[str, float]],
    room: str = "Hall", dock: bool = True,
) -> None:
    """One outing: cleans `room` point by point (confirmed from the 2nd poll on,
    the same debounce pattern the other pipeline tests use), then docks."""
    _poll(coord, _device(duid, vacuum_room_name=room, _path_dry=points[:1]))
    for i in range(2, len(points) + 1):
        clock.advance(minutes=5)
        _poll(coord, _device(duid, vacuum_room_name=room, _path_dry=points[:i]))
    if dock:
        clock.advance(minutes=5)
        _poll(coord, _device(duid, in_cleaning=False))


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


def test_split_job_measures_the_whole_run_not_one_batch(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The regression this file exists for. One job, two batches with a dock trip
    in between, together covering exactly what a single-outing clean covers: the
    persisted % must read 100 % and the learned baseline must not move."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"

    # Reference clean in one outing -> baseline 5 cells.
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5)
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))  # scope cleared -> run harvested
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5

    # The same clean, this time dispatched as 3 cells + 2 cells.
    clock.advance(minutes=5)
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5[:3])
    # Mid-job: nothing harvested yet — no partial % written, baseline untouched.
    assert coord._room_coverage.get("Hall", {}).get("dry") is None
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5

    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL5[3:])
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    assert coord._room_coverage["Hall"]["dry"] == 100  # 5 of 5 cells, both batches
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5  # a full clean, not a batch


def test_run_events_fire_once_per_run(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """``clean_started``/``run_finished`` bracket the RUN; ``clean_finished``
    still fires per sortie because services.py's `_JobRunner` listens on it to
    dispatch the job's next batch — deferring it would wedge every pool task."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d2"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5[:3])
    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL5[3:])
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    names = coord.hass.bus.names()
    assert names.count("anyvac_clean_started") == 1
    assert names.count("anyvac_clean_finished") == 2  # one per batch, unchanged
    assert names.count("anyvac_run_finished") == 1
    run_done = [d for n, d in coord.hass.bus.events if n == "anyvac_run_finished"][-1]
    assert run_done["rooms"] == ["Hall"]  # both batches' rooms, one payload


def test_manual_sortie_without_job_scope_closes_immediately(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Regression guard for everything started outside `anyvac.clean` (Roborock
    app, the card's native command, degraded mode): with no job scope there is
    nothing to wait for, so the run closes on the docking poll exactly as it did
    before docs/36 — same events, same accumulator reset."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d3"
    _sortie(coord, clock, duid, HALL5)

    assert coord._run_pending == {}
    assert coord._room_cells[duid] == {}  # harvested and cleared on the dock poll
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5
    assert coord.hass.bus.names() == [
        "anyvac_clean_started", "anyvac_room_done",
        "anyvac_clean_finished", "anyvac_run_finished",
    ]


def test_midrun_path_reset_keeps_the_coverage_cells(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A firmware path reset inside a run (the robot returning for another pass
    through a room) is stitched for the drawn trace by docs/27 — the coverage
    cells now follow the same verdict instead of being wiped unconditionally."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d4"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5[:3], dock=False)
    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 3

    # The robot's own array restarts (shorter than what we have already seen).
    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL5[3:4]))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL5[3:]))

    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 5
    # ...and the trace is stitched, not bridged: two segments, nothing lost.
    assert [len(s) for s in coord._dry_path[duid]] == [3, 2]


def test_path_reset_outside_a_job_still_wipes_the_cells(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Mirror of the docs/27 guard for the trace: with no job scope a restarted
    trajectory IS an unrelated new clean, so its cells must start over."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d5"
    _sortie(coord, clock, duid, HALL5[:3], dock=False)
    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 3

    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL5[3:4]))
    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 1
    assert coord._dry_path[duid] == [[HALL5[3]]]


def test_drive_through_room_gets_no_live_gauge(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """docs/36: outside an orchestrated job there is no room scope to filter
    plan-transit, so a corridor crossed with the fan on collects real cells. The
    live gauge must not turn those into a per-room %: a room qualifies only once
    it is in the job's scope, debounce-confirmed, or in `cleaned_rooms`."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d6"
    pts = HALL5[:3] + [{"x": 1100, "y": 100}, {"x": 1400, "y": 100}]

    for i in range(1, len(pts) + 1):
        clock.advance(minutes=1)
        _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=pts[:i]))

    # The cells are collected (they are what the trace is made of)...
    assert "Kitchen" in coord._room_cells[duid]
    # ...but only the room actually being cleaned gets a number.
    progress = coord._build_progress(_device(duid, vacuum_room_name="Hall"))
    assert set(progress) == {"Hall"}


def test_stuck_job_scope_cannot_hold_a_run_open_forever(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Safety net: a job runner torn down without its cleanup path leaves the
    scope set. The elapsed cap closes the run anyway, so the coverage % is never
    lost to a scope that will never clear."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d7"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5)  # baseline run
    coord._run_pending.clear()
    coord._harvest_run(_device(duid, in_cleaning=False))
    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL5)
    assert duid in coord._run_pending  # deferred: the scope is still set

    clock.advance(hours=4)  # past _RUN_DEFER_MAX_S
    _poll(coord, _device(duid, in_cleaning=False))
    assert coord._run_pending == {}
    assert coord._room_cells[duid] == {}


def test_job_releasing_the_vacuum_mid_flight_still_closes_the_run(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The job can clear this vacuum's scope while it is still driving home from
    an earlier batch, so a sortie can find a pending entry AND a cleared scope on
    the same poll. The harvest must run there and the run-level event must not be
    swallowed by the leftover pending marker."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d8"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL5[:3])
    assert duid in coord._run_pending  # batch 1 deferred, job still holds the scope

    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL5[3:], dock=False)
    coord.set_job_rooms(duid, None)  # job closes out while the robot drives home
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    assert coord._run_pending == {}
    assert coord.hass.bus.names().count("anyvac_run_finished") == 1
    assert coord._cov_baseline[duid]["Hall"]["dry"] == 5  # whole run, both batches
