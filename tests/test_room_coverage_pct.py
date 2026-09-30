"""Room completion % (docs/45) — replaces the docs/29 baseline tests.

What a room's number means now: the share of the ORDERED work that is done —
the robot footprint over the room's reachable floor for the first pass, path
length relative to that first pass for every further one. And which rooms get
a number at all: only the ones the run really cleaned. A room the robot merely
drives through (not a target, or a target whose turn has not come) never does.

Paths are synthetic lawnmower patterns with the lane spacing measured on the
real fleet (117 mm, docs/45 §3.1). Rooms are bounding boxes here — the
coordinator's fallback geometry; the decoded-grid geometry is covered by the
`coverage` module tests at the bottom.

Harness copied from `test_run_vs_sortie.py` (established per-file duplication
convention in this test suite).
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



ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    {"segment_id": 2, "name": "Kitchen", "x0": 1000, "y0": 0, "x1": 3000, "y1": 1000},
    {"segment_id": 3, "name": "Bath", "x0": 3000, "y0": 0, "x1": 4000, "y1": 1000},
]
SEG = {"Hall": 1, "Kitchen": 2, "Bath": 3}


def lawn(x0: float, x1: float, y0: float = 80, y1: float = 920, lane: float = 117,
         vertical: bool = False) -> list[dict[str, float]]:
    """Boustrophedon lanes, a point every 150 mm (the firmware records ~120 mm)."""
    pts: list[dict[str, float]] = []
    if vertical:
        xs = [x0 + i * lane for i in range(int((x1 - x0) // lane) + 1)]
        for i, x in enumerate(xs):
            ys = [y0 + j * 150 for j in range(int((y1 - y0) // 150) + 1)] + [y1]
            pts += [{"x": x, "y": y} for y in (ys if i % 2 == 0 else ys[::-1])]
        return pts
    ys = [y0 + i * lane for i in range(int((y1 - y0) // lane) + 1)]
    for i, y in enumerate(ys):
        xs = [x0 + j * 150 for j in range(int((x1 - x0) // 150) + 1)] + [x1]
        pts += [{"x": x, "y": y} for x in (xs if i % 2 == 0 else xs[::-1])]
    return pts


def line(a: tuple[float, float], b: tuple[float, float]) -> list[dict[str, float]]:
    n = max(1, int(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 // 150))
    return [{"x": a[0] + (b[0] - a[0]) * i / n, "y": a[1] + (b[1] - a[1]) * i / n} for i in range(n + 1)]


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "dry",
        "vacuum_room_name": None,
        "rooms": [dict(r) for r in ROOMS],
        "cleaned_rooms": [],
        "target_segments": [],
        "repeat": None,
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


def _run(coord, clock, duid, path, *, targets, repeat=None, step=20, until=None, wet=False,
         room_of=None):
    """Feed `path` (one growing firmware trajectory) in polls of `step` points,
    30 s apart. `until` stops early (points not delivered yet)."""
    end = len(path) if until is None else until
    segs = [SEG[t] for t in targets]
    i = 0
    while i < end:
        i = min(end, i + step)
        clock.advance(seconds=30)
        last = path[i - 1]
        room = room_of(last) if room_of else None
        _poll(coord, _device(
            duid, _path_dry=path[:i], _path_wet=path[:i] if wet else [],
            target_segments=segs, repeat=repeat, vacuum_room_name=room,
        ))


def _dock(coord, clock, duid, path=None, **kw):
    clock.advance(seconds=30)
    _poll(coord, _device(duid, in_cleaning=False, _path_dry=path or [], **kw))


def _room_of(p: dict[str, float]) -> str | None:
    for r in ROOMS:
        if r["x0"] <= p["x"] <= r["x1"] and r["y0"] <= p["y"] <= r["y1"]:
            return r["name"]
    return None


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


def test_single_room_clean_reads_100_and_the_live_gauge_climbs(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    path = lawn(80, 920)
    _run(coord, clock, "d1", path, targets=["Hall"], until=len(path) // 2)
    mid = coord._build_progress(_device("d1"))["Hall"]
    assert 30 <= mid["dry_pct"] <= 75
    assert mid["active"] is True and mid["passes"] == 1
    _run(coord, clock, "d1", path, targets=["Hall"])
    _dock(coord, clock, "d1", path)
    assert coord._room_coverage["Hall"]["dry"] == 100
    assert coord._room_coverage["Hall"]["dry_floor"] >= 95
    assert "Hall" in coord.rooms_estimate["d1"]


def test_two_passes_read_half_after_the_first(monkeypatch, clock) -> None:
    """The field report: a 2-pass kitchen read 97 % while the firmware said 50 %."""
    coord = _new_coordinator(monkeypatch, clock)
    p1 = lawn(80, 920)
    p2 = lawn(80, 920, vertical=True)
    path = p1 + p2
    _run(coord, clock, "d1", path, targets=["Hall"], repeat=2, until=len(p1))
    after1 = coord._build_progress(_device("d1"))["Hall"]
    assert 40 <= after1["dry_pct"] <= 55
    assert after1["dry_floor"] >= 95
    _run(coord, clock, "d1", path, targets=["Hall"], repeat=2, until=len(p1) + len(p2) // 2)
    mid2 = coord._build_progress(_device("d1"))["Hall"]
    assert 60 <= mid2["dry_pct"] <= 85
    assert mid2["dry_pass"] == 2
    _run(coord, clock, "d1", path, targets=["Hall"], repeat=2)
    _dock(coord, clock, "d1", path)
    assert coord._room_coverage["Hall"]["dry"] == 100


def test_a_room_that_is_not_a_target_never_gets_a_number(monkeypatch, clock) -> None:
    """User report: % written into rooms the robot only drives through."""
    coord = _new_coordinator(monkeypatch, clock)
    path = line((500, 500), (3200, 500)) + lawn(3080, 3920)
    _run(coord, clock, "d1", path, targets=["Bath"], room_of=_room_of)
    prog = coord._build_progress(_device("d1"))
    assert set(prog) == {"Bath"}
    _dock(coord, clock, "d1", path)
    assert set(coord._room_coverage) == {"Bath"}
    assert coord._last_calib["d1"]["ignored_mm"].keys() >= {"Hall", "Kitchen"}
    assert "Kitchen" not in coord.rooms_estimate.get("d1", {})


def test_a_target_crossed_before_its_turn_is_not_counted(monkeypatch, clock) -> None:
    """Whole-flat run: every room is a target, so the targets alone cannot filter
    the corridor crossing — the sequence (and the dwell) does."""
    coord = _new_coordinator(monkeypatch, clock)
    coord._room_sequence = {"Hall": 1, "Bath": 2, "Kitchen": 3}
    hall = lawn(80, 920)
    cross = line((920, 920), (3080, 500))
    bath = lawn(3080, 3920)
    back = line((3080, 920), (1080, 80))
    kitchen = lawn(1080, 2920)
    path = hall + cross + bath
    _run(coord, clock, "d1", path, targets=["Hall", "Kitchen", "Bath"], room_of=_room_of)
    prog = coord._build_progress(_device("d1"))
    assert "Kitchen" not in prog  # crossed, not cleaned
    assert prog["Hall"]["done"] is True and prog["Hall"]["dry_pct"] == 100
    assert prog["Bath"]["active"] is True
    path = path + back + kitchen
    _run(coord, clock, "d1", path, targets=["Hall", "Kitchen", "Bath"], room_of=_room_of)
    _dock(coord, clock, "d1", path)
    assert {k: v["dry"] for k, v in coord._room_coverage.items()} == {
        "Hall": 100, "Bath": 100, "Kitchen": 100,
    }


def test_the_tail_delivered_with_the_return_poll_is_counted(monkeypatch, clock) -> None:
    """Field report: an empty rectangular Hall read 83 %. The firmware refreshes
    the map on the state change, so the last seconds of cleaning arrive in the
    poll that already says returning_home — they used to be dropped."""
    coord = _new_coordinator(monkeypatch, clock)
    path = lawn(80, 920)
    cut = int(len(path) * 0.75)
    _run(coord, clock, "d1", path, targets=["Hall"], until=cut)
    clock.advance(seconds=30)
    _poll(coord, _device("d1", transit=True, status_state="returning_home",
                         _path_dry=path, target_segments=[1]))
    live = coord._build_progress(_device("d1"))["Hall"]
    assert live["dry_floor"] >= 95
    elapsed_before = dict(coord._room_elapsed["d1"])
    _dock(coord, clock, "d1", path)
    assert coord._room_coverage["Hall"]["dry"] == 100
    # the tail adds coverage only — its time delta stays unattributed
    assert coord._last_calib["d1"]["rooms"]["Hall"]["active_min"] == round(elapsed_before["Hall"] / 60)


def test_an_interrupted_room_keeps_its_partial_completion(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    path = lawn(80, 920)
    _run(coord, clock, "d1", path, targets=["Hall"], until=int(len(path) * 0.4))
    _dock(coord, clock, "d1", path[: int(len(path) * 0.4)])
    pct = coord._room_coverage["Hall"]["dry"]
    assert 20 <= pct <= 70
    rec = coord._last_calib["d1"]["rooms"]["Hall"]["dry"]
    assert rec["accepted"] is False and rec["reason"].startswith("completion")


def test_moving_on_to_the_next_target_finishes_the_room(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    coord._room_sequence = {"Hall": 1, "Bath": 2}
    hall = lawn(80, 920)[: len(lawn(80, 920)) // 2]
    path = hall + line((920, 500), (3080, 500)) + lawn(3080, 3920)
    _run(coord, clock, "d1", path, targets=["Hall", "Bath"], room_of=_room_of)
    _dock(coord, clock, "d1", path)
    assert coord._room_coverage["Hall"]["dry"] == 100


def test_wet_coverage_only_in_the_room_being_cleaned(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    path = line((500, 500), (3200, 500)) + lawn(3080, 3920)
    _run(coord, clock, "d1", path, targets=["Bath"], wet=True, room_of=_room_of)
    prog = coord._build_progress(_device("d1"))
    assert set(prog) == {"Bath"} and prog["Bath"]["wet_pct"] >= 90


def test_reset_learning_clears_the_persisted_completion(monkeypatch, clock) -> None:
    coord = _new_coordinator(monkeypatch, clock)
    path = lawn(80, 920)
    _run(coord, clock, "d1", path, targets=["Hall"])
    _dock(coord, clock, "d1", path)
    assert coord._room_coverage["Hall"]["dry"] == 100
    coord.reset_learning(room="Hall", estimates=False)
    assert "Hall" not in coord._room_coverage


# -- coverage module: decoded-grid geometry ------------------------------------

def test_reachable_floor_drops_slivers_a_robot_cannot_enter() -> None:
    import numpy as np
    from custom_components.anyvac import coverage as cv

    floor = np.zeros((40, 60), dtype=bool)
    floor[5:35, 5:35] = True  # 1.5 x 1.5 m room
    floor[18:20, 35:55] = True  # 100 mm wide slot behind furniture
    reach = cv.reachable(floor)
    assert reach[10:30, 10:30].all()
    assert not reach[18:20, 40:55].any()


def test_grid_geometry_looks_rooms_up_by_pixel_not_bbox() -> None:
    from custom_components.anyvac import coverage as cv
    from custom_components.anyvac import homeframe
    from tests._synthetic_raw_map import make_raw_map

    # An L: segment 1 wraps around segment 2's corner — their bboxes overlap.
    raw = make_raw_map(80, 60, top=0, left=0, rooms={1: (2, 2, 60, 20), 2: (40, 20, 60, 50)})
    grid = homeframe.decode_grid(raw)
    geo = cv.geometry_from_grid(grid, [{"segment_id": 1, "name": "A"}, {"segment_id": 2, "name": "B"}])
    assert geo.room_at(50 * 50, 10 * 50) == "A"
    assert geo.room_at(50 * 50, 30 * 50) == "B"
    assert geo.room_at(10 * 50, 40 * 50) is None  # plain floor, no room
    assert len(geo.rooms["B"].reach) > 0
