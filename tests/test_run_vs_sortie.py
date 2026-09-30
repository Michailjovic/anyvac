"""Tests for RUN vs SORTIE accumulation (docs/36, field bug 2026-09-02).

A job dispatched progressively (docs/23) docks between its batches, and a
firmware path reset can happen mid-clean when the robot comes back for another
pass through a room. Both look like a fresh ``in_cleaning`` edge / a fresh
trajectory, but neither is a new CLEAN — and everything that measures the clean
(the run's `RunTracker` with its footprint cells, per-room active time, the
run's start time, the persisted completion %) has to span the whole run.

Before docs/36 each sortie was harvested as if it were a complete clean, so a
room cleaned across a dock trip persisted a partial %. (The learned "full
clean" baseline this file originally also guarded is gone since docs/45.)

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
        "_store", "_est_store", "_cov_store", "_cov_pct_store", "_cov_legacy_pct_store", "_sel_store",
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
    coord._runs = {}
    coord._job_rooms = {}
    coord._job_seq = 0
    coord._job_id = {}
    coord._path_job_id = {}
    coord._run_pending = {}
    coord._run_targets_seen = {}
    coord._path_seen = {}
    coord._cov_gate = {}
    coord._geo = {}
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


# Hall is the room being cleaned; Kitchen the neighbour the robot only ever
# drives through. Every real segment clean carries its targets in the map's
# BLOCKS (docs/45), so `_device` defaults `target_segments` to Hall.
ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    {"segment_id": 2, "name": "Kitchen", "x0": 1000, "y0": 0, "x1": 2000, "y1": 1000},
]


def _lawn() -> list[dict[str, float]]:
    """Hall cleaned boustrophedon, 117 mm lanes, a point every 150 mm."""
    pts: list[dict[str, float]] = []
    for i in range(8):
        y = 80 + i * 117
        xs = [80 + j * 150 for j in range(6)] + [920]
        pts += [{"x": x, "y": y} for x in (xs if i % 2 == 0 else xs[::-1])]
    return pts


HALL = _lawn()
HALF = len(HALL) // 2


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "dry",
        "vacuum_room_name": None,
        "rooms": [dict(r) for r in ROOMS],
        "cleaned_rooms": [],
        "target_segments": [1],
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
    room: str = "Hall", dock: bool = True, step: int = 8,
) -> None:
    """One outing: cleans `room` in polls of `step` new points, 1 min apart,
    then docks."""
    i = 0
    while i < len(points):
        i = min(len(points), i + step)
        _poll(coord, _device(duid, vacuum_room_name=room, _path_dry=points[:i]))
        clock.advance(minutes=1)
    if dock:
        clock.advance(minutes=1)
        _poll(coord, _device(duid, in_cleaning=False))


def _hall_cells(coord: AnyVacCoordinator, duid: str) -> int:
    run = coord._runs.get(duid)
    kr = ((run.rooms if run else {}).get("Hall") or {}).get("dry")
    return len(kr.cells) if kr else 0


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


def test_split_job_measures_the_whole_run_not_one_batch(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The regression this file exists for. One job, two batches with a dock trip
    in between, together covering exactly what a single-outing clean covers: the
    persisted % must read 100 %, and nothing may be persisted mid-job."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL[:HALF])
    # Mid-job: nothing harvested yet — no partial % written.
    assert coord._room_coverage.get("Hall", {}).get("dry") is None
    assert duid in coord._run_pending

    clock.advance(minutes=5)
    # The firmware restarts its trajectory for the second batch.
    _sortie(coord, clock, duid, HALL[HALF:])
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    assert coord._room_coverage["Hall"]["dry"] == 100  # both batches together
    assert coord._room_coverage["Hall"]["dry_floor"] >= 95


def test_run_events_fire_once_per_run(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """``clean_started``/``run_finished`` bracket the RUN; ``clean_finished``
    still fires per sortie because services.py's `_JobRunner` listens on it to
    dispatch the job's next batch — deferring it would wedge every pool task."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d2"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL[:HALF])
    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL[HALF:])
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
    _sortie(coord, clock, duid, HALL)

    assert coord._run_pending == {}
    assert _hall_cells(coord, duid) == 0  # harvested and cleared on the dock poll
    assert coord._room_coverage["Hall"]["dry"] == 100
    assert coord.hass.bus.names() == [
        "anyvac_clean_started", "anyvac_room_done",
        "anyvac_clean_finished", "anyvac_run_finished",
    ]


def test_midrun_path_reset_keeps_the_coverage_cells(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A firmware path reset inside a run (the robot returning for another pass
    through a room) is stitched for the drawn trace by docs/27 — the run's
    coverage follows the same verdict instead of being wiped."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d4"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL[:HALF], dock=False)
    before = _hall_cells(coord, duid)
    assert before > 0

    # The robot's own array restarts (shorter than what we have already seen).
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL[HALF:HALF + 1]))
    clock.advance(minutes=1)
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL[HALF:]))

    assert _hall_cells(coord, duid) > before
    # ...and the trace is stitched, not bridged: two segments, nothing lost.
    assert sum(len(s) for s in coord._dry_path[duid]) == len(HALL)
    assert len(coord._dry_path[duid]) == 2


def test_path_reset_outside_a_job_still_wipes_the_cells(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Mirror of the docs/27 guard for the trace: with no job scope a restarted
    trajectory IS an unrelated new clean, so its coverage must start over."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d5"
    _sortie(coord, clock, duid, HALL[:HALF], dock=False)
    assert _hall_cells(coord, duid) > 0

    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=HALL[HALF:HALF + 1]))
    assert _hall_cells(coord, duid) == 0  # a fresh run: not even activated yet
    assert coord._dry_path[duid] == [[HALL[HALF]]]


def test_drive_through_room_gets_no_live_gauge(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """docs/36 → docs/45: a corridor crossed with the fan on is not a target of
    the run, so it never becomes coverage and never gets a number."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d6"
    pts = HALL + [{"x": 1100 + 150 * i, "y": 500} for i in range(5)]
    _sortie(coord, clock, duid, pts, dock=False)
    progress = coord._build_progress(_device(duid, vacuum_room_name="Hall"))
    assert set(progress) == {"Hall"}
    assert coord._runs[duid].ignored.get("Kitchen", 0) > 0


def test_stuck_job_scope_cannot_hold_a_run_open_forever(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Safety net: a job runner torn down without its cleanup path leaves the
    scope set. The elapsed cap closes the run anyway, so the completion % is
    never lost to a scope that will never clear."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d7"
    coord.set_job_rooms(duid, {"Hall"})
    _sortie(coord, clock, duid, HALL)
    assert duid in coord._run_pending  # deferred: the scope is still set
    assert coord._room_coverage.get("Hall") is None

    clock.advance(hours=4)  # past _RUN_DEFER_MAX_S
    _poll(coord, _device(duid, in_cleaning=False))
    assert coord._run_pending == {}
    assert coord._room_coverage["Hall"]["dry"] == 100
    assert _hall_cells(coord, duid) == 0


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
    _sortie(coord, clock, duid, HALL[:HALF])
    assert duid in coord._run_pending  # batch 1 deferred, job still holds the scope

    clock.advance(minutes=5)
    _sortie(coord, clock, duid, HALL[HALF:], dock=False)
    coord.set_job_rooms(duid, None)  # job closes out while the robot drives home
    clock.advance(minutes=1)
    _poll(coord, _device(duid, in_cleaning=False))

    assert coord._run_pending == {}
    assert coord.hass.bus.names().count("anyvac_run_finished") == 1
    assert coord._room_coverage["Hall"]["dry"] == 100  # whole run, both batches
