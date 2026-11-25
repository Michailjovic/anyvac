"""Tests for the per-TASK lifecycle (docs/52 §9.3, 1.60.0).

One task = one command the robot runs to its end: one room in a room-by-room
job. The firmware chains tasks without dropping ``in_cleaning`` — the robot
gets its next room on its way home, and ``in_cleaning`` also stays on through
the dock's mop wash at the end of a task (S8 field history 2026-11-25). So the
end of a task is the edge into ``returning_home`` (unless the battery is low:
that is a mid-task recharge trip), or the fall of ``in_cleaning``.

Every task is harvested on its own — one clean measurement per room, never a
mix of two (docs/52 O3). This replaces docs/36's deferred harvest, which
spanned a progressive job's batches. The drawn trace still stitches across
the whole job (docs/27).

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
    coord._pending_saves = {}
    coord.hass = _FakeHass()
    for attr in (
        "_store", "_est_store", "_cov_store", "_cov_pct_store", "_cov_legacy_pct_store", "_sel_store",
        "_pins_store", "_seq_store", "_layers_store", "_paths_store", "_last_run_store",
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
    coord._signal_candidate = {}
    coord._last_run = {}
    coord._last_run_store = _FakeStore()
    coord._job_state = None
    coord._last_calib = {}
    coord._selected_rooms = set()
    coord._room_pins = {}
    coord._room_elapsed = {}
    coord._last_poll = {}
    coord._runs = {}
    coord._job_rooms = {}
    coord._job_seq = 0
    coord._job_id = {}
    coord._path_job_id = {}
    coord._prev_state = {}
    coord._task_open = {}
    coord._task_item = {}
    coord._task_last = {}
    coord._task_rooms_done = {}
    coord._harvest_pending = {}
    coord.app_sequence = {}
    coord._app_sequence_at = {}
    coord._job_store = _FakeStore() if "_FakeStore" in globals() else None
    coord._job_saved = None
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
    coord._maybe_harvest_pending(device)


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


def _lawn_at(x0: float) -> list[dict[str, float]]:
    return [{"x": p["x"] + x0, "y": p["y"]} for p in HALL]


KITCHEN = _lawn_at(1000)


def _clean(coord, clock, duid, points, room, seg, step=8, **kw):
    i = 0
    while i < len(points):
        i = min(len(points), i + step)
        _poll(coord, _device(duid, vacuum_room_name=room, target_segments=[seg],
                             status_state="segment_cleaning", battery=80, _path_dry=points[:i], **kw))
        clock.advance(minutes=1)


def _return(coord, clock, duid, points, seg, battery=80, then=True):
    """The return edge, and (`then`) the next map snapshot on the way home."""
    _poll(coord, _device(duid, status_state="returning_home", transit=True, target_segments=[seg],
                         battery=battery, _path_dry=points))
    clock.advance(seconds=20)
    if then:
        _poll(coord, _device(duid, status_state="returning_home", transit=True, target_segments=[seg],
                             battery=battery, _path_dry=points))
        clock.advance(seconds=20)


def test_return_edge_ends_the_task_and_harvests_it(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    coord.set_job_rooms("s6", {"Hall", "Kitchen"})
    coord.set_task_item("s6", {"Hall"})
    _clean(coord, clock, "s6", HALL, "Hall", 1)
    _return(coord, clock, "s6", HALL, 1)
    finished = [d for n, d in coord.hass.bus.events if n == "anyvac_clean_finished"]
    assert len(finished) == 1
    (row,) = finished[0]["rooms_result"]
    assert row["room"] == "Hall" and row["cleaned"] is True and row["dry"] == 100
    assert coord._room_coverage["Hall"]["dry"] == 100
    assert coord._last_calib["s6"]["rooms"]["Hall"]["dry"]["accepted"] is True
    # Inside a job the runner reports the vacuum, not the coordinator.
    assert "anyvac_run_finished" not in coord.hass.bus.names()
    # The task's room is reported done even though `in_cleaning` never fell.
    assert [d["room"] for n, d in coord.hass.bus.events if n == "anyvac_room_done"] == ["Hall"]


def test_chained_task_is_measured_on_its_own_and_the_trace_stitches(monkeypatch, clock) -> None:
    """The next room is taken on the way home: no `in_cleaning` edge, a
    fresh firmware trajectory, a fresh measurement — one trace."""
    coord = _new_coordinator(monkeypatch, clock)
    coord.set_job_rooms("s6", {"Hall", "Kitchen"})
    coord.set_task_item("s6", {"Hall"})
    _clean(coord, clock, "s6", HALL, "Hall", 1)
    _return(coord, clock, "s6", HALL, 1, then=False)  # next room before the next map
    first_run = coord._runs["s6"]
    coord.set_task_item("s6", {"Kitchen"})
    _clean(coord, clock, "s6", KITCHEN, "Kitchen", 2)
    assert coord._runs["s6"] is not first_run
    assert "Hall" not in coord._runs["s6"].rooms
    _return(coord, clock, "s6", KITCHEN, 2)
    finished = [d for n, d in coord.hass.bus.events if n == "anyvac_clean_finished"]
    assert [r["room"] for r in finished[1]["rooms_result"]] == ["Kitchen"]
    assert coord._last_calib["s6"]["rooms"]["Kitchen"]["dry"]["accepted"] is True
    # docs/27: both rooms stay drawn — the job did not change.
    assert len(coord._dry_path["s6"]) == 2


def test_low_battery_trip_home_is_not_the_end_of_the_task(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    _clean(coord, clock, "s6", HALL[:HALF], "Hall", 1)
    _return(coord, clock, "s6", HALL[:HALF], 1, battery=19)
    assert "anyvac_clean_finished" not in coord.hass.bus.names()
    assert coord._task_open["s6"] is True


def test_without_a_job_run_finished_waits_for_the_dock(monkeypatch, clock) -> None:
    """A clean started from the app: the task ends on the way home (learning
    happens then), `run_finished` once `in_cleaning` falls after the wash."""
    coord = _new_coordinator(monkeypatch, clock)
    _clean(coord, clock, "s6", HALL, "Hall", 1)
    _return(coord, clock, "s6", HALL, 1)
    assert coord.hass.bus.names().count("anyvac_clean_finished") == 1
    assert "anyvac_run_finished" not in coord.hass.bus.names()
    _poll(coord, _device("s6", status_state="washing_the_mop", transit=True, _path_dry=HALL))
    _poll(coord, _device("s6", in_cleaning=False, status_state="charging", _path_dry=HALL))
    assert coord.hass.bus.names().count("anyvac_run_finished") == 1
    assert coord.hass.bus.names().count("anyvac_clean_finished") == 1


def test_a_clean_that_just_stops_closes_on_the_fall(monkeypatch, clock) -> None:
    """S8 out of water: `idle` mid-room, `in_cleaning` falls — no return edge."""
    coord = _new_coordinator(monkeypatch, clock)
    _clean(coord, clock, "s6", HALL[:HALF], "Hall", 1)
    _poll(coord, _device("s6", in_cleaning=False, status_state="idle", _path_dry=HALL[:HALF]))
    names = coord.hass.bus.names()
    assert names.count("anyvac_clean_finished") == 1 and names.count("anyvac_run_finished") == 1
    assert 20 <= coord._room_coverage["Hall"]["dry"] < 100


def test_a_discarded_task_learns_nothing(monkeypatch, clock) -> None:
    """docs/52 §3.4: a room removed mid-clean is never calibrated."""
    coord = _new_coordinator(monkeypatch, clock)
    coord.set_job_rooms("s6", {"Hall"})
    _clean(coord, clock, "s6", HALL[:HALF], "Hall", 1)
    coord.discard_task("s6")
    assert coord._runs["s6"].rooms == {}
    assert coord._room_elapsed["s6"] == {}


def test_water_off_leaves_no_wet_evidence(monkeypatch, clock) -> None:
    """docs/52 O3 (S7 MaxV): mop holder down, water off — mop points, no wet floor."""
    coord = _new_coordinator(monkeypatch, clock)
    _clean(coord, clock, "s7", HALL, "Hall", 1, _path_wet=HALL, water_off=True)
    run = coord._runs["s7"]
    assert run.floor_fraction(coord._geo["s7"][2], "Hall", "wet") == 0
    assert run.floor_fraction(coord._geo["s7"][2], "Hall", "dry") > 0.9


def test_the_last_seconds_arriving_after_the_return_edge_are_counted(monkeypatch, clock) -> None:
    """Field 2026-11-25 (S7 MaxV Bathroom read 71 %): the map with a room's
    last seconds came after the robot turned home. The task is reported at
    the edge, learned with the next snapshot."""
    coord = _new_coordinator(monkeypatch, clock)
    cut = int(len(HALL) * 0.7)
    _clean(coord, clock, "s7", HALL[:cut], "Hall", 1)
    _poll(coord, _device("s7", status_state="returning_home", transit=True, target_segments=[1],
                         battery=80, _path_dry=HALL[:cut]))
    assert coord.hass.bus.names().count("anyvac_clean_finished") == 1
    assert "Hall" not in coord._room_coverage  # not learned yet
    clock.advance(seconds=30)
    _poll(coord, _device("s7", status_state="returning_home", transit=True, target_segments=[1],
                         battery=80, _path_dry=HALL))
    assert coord._room_coverage["Hall"]["dry"] == 100
    assert coord._last_calib["s7"]["rooms"]["Hall"]["dry"]["accepted"] is True
