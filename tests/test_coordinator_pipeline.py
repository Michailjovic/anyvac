"""Tests for AnyVacCoordinator's per-poll pipeline (docs/14 kanon Fáze 4).

Covers the four scenarios flagged in CLAUDE.md as untested: a mid-clean mop wash
freezing attribution, a drive-through room being rejected at calibration time, a
single session calibrating multiple rooms (docs/16 continuous calibration), and
per-duid state isolation between two independently-owned vacuums ("two
households" sharing one HA instance).

None of these touch the real Roborock integration (`_extract_device`) — that
coupling is exercised in the field, not here. What's tested is the pure
in-memory pipeline `_async_update_data` drives every poll: `_update_history`,
`_detect_room_done`, `_track_and_emit`, `_attribute_points`, in that exact
order (mirrored by the `_poll()` helper below). Like
`test_planner_timeline.py`'s `_planner()`, the coordinator is built via
`object.__new__` to skip `__init__` (real `hass` + `Store` + device registry
are irrelevant to this computation) — only the instance attributes the
pipeline methods actually touch are initialised, so a change that starts
touching untouched state fails loudly (AttributeError) instead of silently
passing.

`dt_util.utcnow` is monkeypatched to a manually-advanced clock so poll deltas
(elapsed-time attribution, the calibration active-time floor) are exact
instead of depending on wall time. All fixtures advance the clock in
5-minute steps deliberately: `_attribute_points` treats a >600s poll gap as a
restart/large gap and drops the delta entirely (see `test_two_households_...`
below, which hit exactly that while prototyping — a useful reminder that the
30s `SCAN_INTERVAL_SECONDS` polling assumption is load-bearing here).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import custom_components.anyvac.coordinator as coordinator_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice

UTC = timezone.utc


class _FakeBus:
    """Records fired events instead of touching a real HA event bus."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()


class _FakeStore:
    """No-op stand-in for homeassistant.helpers.storage.Store — these tests
    exercise only the in-memory pipeline; persistence is out of scope."""

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        return None


class _Clock:
    """Deterministic, manually-advanced replacement for dt_util.utcnow()."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def _new_coordinator(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> AnyVacCoordinator:
    """Bare coordinator with only the pipeline's own state initialised."""
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
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock.now)
    return coord


# Two non-overlapping room bboxes (mm) with a strip of plain floor between them
# (1000..2000 mm belongs to no room). Paths are boustrophedon lanes 117 mm apart
# with a point every 150 mm — what the firmware records (docs/45 §3.1) — so a
# room's completion is real footprint coverage, not a handful of sample points.
ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    {"segment_id": 2, "name": "Bathroom", "x0": 2000, "y0": 0, "x1": 3000, "y1": 1000},
]


def _lawn(x0: float) -> list[dict[str, float]]:
    """A full clean of the 1 m room starting at `x0`."""
    pts: list[dict[str, float]] = []
    for i in range(8):
        y = 80 + i * 117
        xs = [x0 + 80 + j * 150 for j in range(6)] + [x0 + 920]
        pts += [{"x": x, "y": y} for x in (xs if i % 2 == 0 else xs[::-1])]
    return pts


HALL = _lawn(0)
BATH = _lawn(2000)


def _device(duid: str, **overrides: Any) -> AnyVacDevice:
    """A device.data payload shaped like what `_extract_device` would produce,
    with sane cleaning defaults overridable per poll."""
    data: dict[str, Any] = {
        "in_cleaning": True,
        "transit": False,
        "vacuuming": True,
        "clean_type": "dry",
        "vacuum_room_name": None,
        "rooms": [dict(r) for r in ROOMS],
        "cleaned_rooms": [],
        "target_segments": [1, 2],
        "_path_dry": [],
        "_path_wet": [],
    }
    data.update(overrides)
    return AnyVacDevice(duid=duid, slug=duid, name=duid, data=data)


def _poll(coord: AnyVacCoordinator, device: AnyVacDevice) -> None:
    """Mirrors `_async_update_data`'s per-device call order exactly — order
    matters (e.g. a session-start reset in `_track_and_emit` must run before
    `_attribute_points` sees that same poll's new points)."""
    coord._update_history(device)
    coord._detect_room_done(device)
    coord._track_and_emit(device)
    coord._attribute_points(device)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))


def test_mop_wash_freezes_attribution_and_room_done(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A mid-clean mop wash (`transit=True`, still `in_cleaning=True`) must not
    accrue elapsed time or fire `anyvac_room_done` — docs/13 A1+A2 / docs/14
    rule 4: HA maps mop-wash to `docked`, so only our own `transit` flag can
    protect the room confirmation and single-room calibration from a false
    "left the room" read mid-wash.

    docs/45 changed ONE thing here on purpose: the new points that arrive with
    the transit poll ARE credited as coverage (restricted to the room being
    cleaned) — the firmware refreshes the map on that state change, so they are
    the last seconds of cleaning before the robot turned for the dock. Their
    time delta still stays unattributed, so the learned estimate is exactly the
    two genuinely-cleaning deltas (5 min + 5 min), not the 20 min wall clock."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"
    dev = lambda n, **kw: _device(duid, vacuum_room_name="Hall", target_segments=[1],
                                  _path_dry=HALL[:n], **kw)

    _poll(coord, dev(12))
    assert coord._room_elapsed.get(duid, {}) == {}  # first poll has no "last" to diff against

    clock.advance(minutes=5)
    _poll(coord, dev(28))
    assert coord._confirmed_room[duid] == "Hall"
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(300.0)
    cells_before = len(coord._runs[duid].rooms["Hall"]["dry"].cells)

    # Poll C (+5 min 6 s): mop wash starts; the trajectory grew by 12 points.
    clock.advance(minutes=5, seconds=6)
    _poll(coord, dev(40, transit=True, vacuuming=False))
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(300.0)  # no time
    assert len(coord._runs[duid].rooms["Hall"]["dry"].cells) == cells_before  # vacuuming off
    assert coord._path_seen[duid]["dry"] == 40  # seen once, never replayed
    assert coord.hass.bus.names() == ["anyvac_clean_started"]  # no room_done during transit
    assert coord._confirmed_room[duid] == "Hall"

    # Poll D (+5 min): the wash ends, cleaning resumes and finishes the room.
    clock.advance(minutes=5)
    _poll(coord, dev(len(HALL)))
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(600.0)

    # Poll E (+5 min): docks -> room_done + history stamp; calibrates 10 minutes.
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False, target_segments=[1], _path_dry=HALL))
    assert coord.hass.bus.names() == [
        "anyvac_clean_started", "anyvac_room_done", "anyvac_clean_finished",
        # docs/36: with no orchestrated job scope the run closes on the same poll,
        # so its run-level event follows the sortie's immediately.
        "anyvac_run_finished",
    ]
    finished = coord.hass.bus.events[-1][1]
    assert finished["duration_min"] == 20  # wall-clock session length, includes the wash
    assert finished["calibrated"] == {"Hall": {"dry": {"before": None, "after": 10}}}
    assert coord.rooms_estimate[duid]["Hall"]["dry"] == 10


def test_transit_drive_through_not_counted_as_completed(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A robot whose dock sits in (or whose path merely crosses) a room it does
    not clean this run must not get that room calibrated, stamped or released
    as done. docs/45: the room is not in the run's targets (map BLOCKS), so its
    points are ignored at attribution time and it never becomes coverage."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d2"
    path = HALL[:14] + BATH  # drives out through Hall, then cleans Bathroom
    dev = lambda n: _device(duid, vacuum_room_name="Bathroom", target_segments=[2],
                            _path_dry=path[:n])
    _poll(coord, dev(20))
    clock.advance(minutes=5)
    _poll(coord, dev(40))
    assert coord._confirmed_room[duid] == "Bathroom"
    clock.advance(minutes=5)
    _poll(coord, dev(len(path)))
    assert "Hall" not in coord._runs[duid].rooms
    assert coord._runs[duid].ignored["Hall"] > 0

    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False, target_segments=[2], _path_dry=path))

    rooms = coord._last_calib[duid]["rooms"]
    assert rooms["Bathroom"]["dry"]["accepted"] is True
    assert "Hall" not in rooms
    assert "Hall" in coord._last_calib[duid]["ignored_mm"]
    assert "Hall" not in coord.rooms_estimate.get(duid, {})
    assert coord.rooms_estimate[duid]["Bathroom"]["dry"] > 0
    assert "Hall" not in coord._room_coverage
    room_done_rooms = [e["room"] for name, e in coord.hass.bus.events if name == "anyvac_room_done"]
    assert room_done_rooms == ["Bathroom"]


def test_multi_room_calibration_in_one_session(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Docs/16 continuous calibration: EVERY completed room of a session is a
    calibration sample, not just a dedicated single-room clean. A session that
    cleans Hall then Bathroom must calibrate both, with each room's own active
    time — not the whole session's duration split evenly, and not just the
    last room cleaned."""
    coord = _new_coordinator(monkeypatch, clock)
    coord._room_sequence = {"Hall": 1, "Bathroom": 2}
    duid = "d3"
    path = HALL + BATH
    half = len(HALL) // 2

    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=path[:half]))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=path[:len(HALL)]))
    assert coord._confirmed_room[duid] == "Hall"

    # Robot moves on to Bathroom; new points now land there instead of Hall.
    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Bathroom", _path_dry=path[:len(HALL) + half]))
    # Bathroom not yet confirmed (only 1 consecutive poll) — Hall's room_done
    # hasn't fired yet either.
    assert coord._confirmed_room[duid] == "Hall"

    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Bathroom", _path_dry=path))
    # Bathroom now confirmed (2nd consecutive) -> Hall's room_done fires ("left").
    assert coord._confirmed_room[duid] == "Bathroom"
    assert coord.hass.bus.names() == ["anyvac_clean_started", "anyvac_room_done"]
    assert coord.hass.bus.events[-1][1]["room"] == "Hall"

    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False, _path_dry=path))

    finished = coord.hass.bus.events[-1][1]
    assert sorted(finished["rooms"]) == ["Bathroom", "Hall"]
    assert finished["calibrated"] == {
        "Hall": {"dry": {"before": None, "after": 5}},
        "Bathroom": {"dry": {"before": None, "after": 10}},
    }
    assert coord.rooms_estimate[duid]["Hall"]["dry"] == 5
    assert coord.rooms_estimate[duid]["Bathroom"]["dry"] == 10
    assert coord._room_coverage["Hall"]["dry"] == 100  # left for the next target
    assert coord._room_coverage["Bathroom"]["dry"] == 100
    # A second `anyvac_room_done` for Bathroom fires on docking.
    room_done_rooms = [e["room"] for name, e in coord.hass.bus.events if name == "anyvac_room_done"]
    assert room_done_rooms == ["Hall", "Bathroom"]


def test_plan_scope_transit_labeling(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """Docs/17 §1.3: a room outside the CURRENT JOB's scope must be treated as
    transit at attribution time — even when the vacuum's raw state is NOT a
    TRANSIT_STATE at all (`transit=False`, genuinely "cleaning" per the
    firmware). docs/45: the job scope is united with the map's BLOCKS into the
    run's targets; a room outside them is ignored for the whole run, and the
    next run (new targets) attributes it normally again."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d4"
    coord.set_job_rooms(duid, {"Bathroom"})
    path = HALL[:20] + BATH

    _poll(coord, _device(duid, vacuum_room_name="Bathroom", target_segments=[],
                         _path_dry=path[:30]))
    assert "Hall" not in coord._runs[duid].rooms
    assert coord._runs[duid].ignored["Hall"] > 0

    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Bathroom", target_segments=[],
                         _path_dry=path))
    # Elapsed time went entirely to Bathroom.
    assert coord._room_elapsed[duid] == {"Bathroom": pytest.approx(300.0)}
    assert "Hall" not in coord._runs[duid].rooms

    # Job over, robot docks; a later manual run of Hall is attributed normally —
    # the room is not blacklisted beyond the run it was out of scope for.
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False, target_segments=[], _path_dry=path))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, vacuum_room_name="Hall", target_segments=[1], _path_dry=HALL))
    assert "Hall" in coord._runs[duid].rooms


def test_two_households_share_one_coordinator(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Two independently-owned vacuums (two Roborock accounts/homes, both added
    to the same HA instance -> one shared AnyVacCoordinator, docs/14) must not
    cross-pollute PER-DUID state even when they coincidentally name a room the
    same thing ("Hall" in both homes here). Learned time estimates, sessions,
    and run trackers are all keyed by duid and must stay isolated.

    `_history` (last-cleaned timestamps) is the one deliberate exception —
    it's documented as "aggregates across all vacuums" (keyed by room NAME
    only, for the same-household multi-robot case) — so this test also pins
    down that a genuine cross-household name collision DOES merge the
    last-cleaned stamp. That's a known, currently-accepted tradeoff, not a
    bug this test is asserting should be fixed; it exists so a future change
    to `_history`'s keying is a deliberate decision, not a silent regression
    either way."""
    coord = _new_coordinator(monkeypatch, clock)
    q = len(HALL) // 4

    # Household A: a fast robot, 5-minute Hall clean.
    _poll(coord, _device("home-a", vacuum_room_name="Hall", target_segments=[1], _path_dry=HALL[:2 * q]))
    clock.advance(minutes=5)
    _poll(coord, _device("home-a", vacuum_room_name="Hall", target_segments=[1], _path_dry=HALL))
    clock.advance(minutes=1)
    _poll(coord, _device("home-a", in_cleaning=False, target_segments=[1], _path_dry=HALL))

    # Household B: also has a room called "Hall" — a slower robot, 15 minutes.
    clock.advance(minutes=1)
    for n in (q, 2 * q, 3 * q, len(HALL)):
        _poll(coord, _device("home-b", vacuum_room_name="Hall", target_segments=[1], _path_dry=HALL[:n]))
        clock.advance(minutes=5)
    clock.advance(minutes=-4)
    _poll(coord, _device("home-b", in_cleaning=False, target_segments=[1], _path_dry=HALL))

    # Per-duid learned estimates stayed independent despite the identical room name.
    assert coord.rooms_estimate["home-a"]["Hall"]["dry"] == 5
    assert coord.rooms_estimate["home-b"]["Hall"]["dry"] == 15
    # Session/tracking state never leaked across duids either (both reset at
    # their own session end, independently).
    assert coord._session_rooms["home-a"] == set()
    assert coord._session_rooms["home-b"] == set()
    assert coord._runs["home-a"].rooms == {}
    assert coord._runs["home-b"].rooms == {}

    # The one deliberately-shared piece of state: last-cleaned-by-name history
    # merges the two homes' same-named room into a single stamp (home-b
    # finished last, so its timestamp wins).
    assert list(coord._history.keys()) == ["Hall"]
    assert coord._history["Hall"]["dry"] == clock.now.isoformat()


def test_path_stitches_across_job_sorties(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """Docs/27: a robot that docks and re-departs mid-JOB (progressive dispatch,
    docs/23 — a pool task's second batch) must keep its dry AND wet trace from
    the first sortie instead of wiping it, so the card can draw the whole
    orchestrated job as one continuous pass. The job boundary is `set_job_rooms`
    (docs/17 §1.3), not the `in_cleaning` transition — `_JobRunner` sets it once
    at job start and clears it once at job finish/cancel, regardless of how many
    sorties happen in between."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d6"
    coord.set_job_rooms(duid, {"Hall"})

    # Sortie 1: one dry + one wet point.
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}], _path_wet=[{"x": 100, "y": 100}],
    ))
    assert coord._dry_path[duid] == [[{"x": 100, "y": 100}]]
    assert coord._wet_path[duid] == [[{"x": 100, "y": 100}]]

    # Sortie 1 ends (dock trip between pool-dispatch batches) — the job itself
    # is still running (`_job_rooms` untouched, only `_JobRunner.finish()` at
    # the whole job's end would clear it).
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    assert coord._dry_path[duid] == [[{"x": 100, "y": 100}]]  # untouched by session-end
    assert coord._wet_path[duid] == [[{"x": 100, "y": 100}]]
    run_sortie1 = coord._runs[duid]

    # Sortie 2 starts: the robot's own raw arrays restart near-empty (real
    # firmware behaviour) — here just a single new point each layer. Must
    # become a NEW segment appended to the existing trace, not a wipe.
    clock.advance(minutes=1)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 200, "y": 100}], _path_wet=[{"x": 200, "y": 100}],
    ))
    assert coord._dry_path[duid] == [[{"x": 100, "y": 100}], [{"x": 200, "y": 100}]]
    assert coord._wet_path[duid] == [[{"x": 100, "y": 100}], [{"x": 200, "y": 100}]]

    # docs/36 (this assertion was the exact opposite until 2026-09-02): the RUN's
    # accumulators now follow the same lifetime as the trace above. A dock trip
    # inside one job is not a new clean, so the run keeps its original start time
    # and its coverage cells instead of restarting from zero — the per-sortie
    # reset was what made a room's coverage % read a partial number and then drag
    # its learned baseline down to the size of a single batch.
    assert coord._session_start[duid] == clock.now - timedelta(minutes=6)
    assert coord._runs[duid] is run_sortie1


def test_path_resets_across_sorties_without_job_scope(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Regression pojistka: outside an active job (degraded mode / manual
    per-vacuum start / raw `anyvac.run_job`), a sortie restart is a genuinely
    new, unrelated session — today's wipe-on-restart behaviour must stand."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d7"
    # No set_job_rooms() call — same sequence as the stitching test above.

    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}], _path_wet=[{"x": 100, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    clock.advance(minutes=1)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 200, "y": 100}], _path_wet=[{"x": 200, "y": 100}],
    ))
    assert coord._dry_path[duid] == [[{"x": 200, "y": 100}]]
    assert coord._wet_path[duid] == [[{"x": 200, "y": 100}]]


def test_path_wipes_on_a_genuinely_new_job(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """Field bug (2026-07-26): `set_job_rooms` becomes truthy again the moment
    ANY new `anyvac.clean` job starts — including a brand new, unrelated job
    for a vacuum that already ran inside some earlier job. A bare
    `duid in self._job_rooms` check (the pre-fix code) cannot tell that case
    apart from a sortie restart WITHIN the same job, so the trace never got
    wiped again once a vacuum had run inside any orchestrated job even once —
    reported as S8's path never clearing across repeated whole-home runs.

    `_sortie_is_new_job` fixes this with a monotonic job id: job A's sortie
    restarts stitch (as `test_path_stitches_across_job_sorties` covers), but
    job B — a completely separate `set_job_rooms` call after job A finished —
    must wipe the trace on its own first sortie, exactly like the no-job-scope
    case, not stitch onto job A's leftover trace."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d8"

    # Job A: one sortie, one point, then the job finishes (job_rooms cleared,
    # mirroring _JobRunner.finish()) — the trace is left on screen per docs/27.
    coord.set_job_rooms(duid, {"Hall"})
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}], _path_wet=[{"x": 100, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    coord.set_job_rooms(duid, None)
    assert coord._dry_path[duid] == [[{"x": 100, "y": 100}]]
    assert coord._wet_path[duid] == [[{"x": 100, "y": 100}]]

    # Job B: a brand new `anyvac.clean` call for the SAME vacuum/room, started
    # some time later — a fresh `set_job_rooms` call, exactly like
    # `_JobRunner.start()` issues for every new job regardless of what ran
    # before. Its first sortie must wipe job A's leftover trace, not stitch a
    # second segment onto it.
    clock.advance(minutes=10)
    coord.set_job_rooms(duid, {"Hall"})
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 900, "y": 900}], _path_wet=[{"x": 900, "y": 900}],
    ))
    assert coord._dry_path[duid] == [[{"x": 900, "y": 900}]]
    assert coord._wet_path[duid] == [[{"x": 900, "y": 900}]]

    # Job B's own second sortie (progressive dispatch) still stitches, same as
    # job A's did — the fix doesn't turn off within-job stitching.
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    clock.advance(minutes=1)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 950, "y": 900}], _path_wet=[{"x": 950, "y": 900}],
    ))
    assert coord._dry_path[duid] == [[{"x": 900, "y": 900}], [{"x": 950, "y": 900}]]
    assert coord._wet_path[duid] == [[{"x": 900, "y": 900}], [{"x": 950, "y": 900}]]
