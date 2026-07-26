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
        "_store", "_est_store", "_cov_store", "_sel_store",
        "_pins_store", "_seq_store", "_layers_store",
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
    coord._dry_path = {}
    coord._dry_path_open = {}
    coord._wet_path = {}
    coord._wet_path_open = {}
    coord._known_duids = set()
    coord._pipeline_warned = False
    coord._view_layers = {"dry": True, "wet": False}
    coord._debug_seen = {}
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: clock.now)
    return coord


# Two non-overlapping room bboxes (mm), sized so a handful of well-spread points
# clear the coverage-evidence floor (>=3 distinct 250mm cells, docs/16 §_evidence_kinds)
# without needing a learned baseline.
ROOMS = [
    {"segment_id": 1, "name": "Hall", "x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    {"segment_id": 2, "name": "Bathroom", "x0": 2000, "y0": 0, "x1": 3000, "y1": 1000},
]


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
    accrue coverage cells or elapsed time, and must not fire `anyvac_room_done`
    — docs/13 A1+A2 / docs/14 rule 4: HA maps mop-wash to `docked`, so only our
    own `transit` flag can protect the room confirmation and single-room
    calibration from a false "left the room" read mid-wash.

    The gap's OWN duration (poll C -> poll D, 5 min 6 s) must not appear in the
    final estimate either — the mop-wash poll's delta is dropped entirely
    rather than deferred, so the learned estimate should reflect exactly the
    two genuinely-cleaning deltas (5 min + 5 min), not the wall-clock span of
    the whole session (20 min)."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d1"

    # Poll A: first ever poll, cleaning Hall, one point (cell (0,0)).
    _poll(coord, _device(duid, vacuum_room_name="Hall", _path_dry=[{"x": 100, "y": 100}]))
    assert coord._room_elapsed.get(duid, {}) == {}  # first poll has no "last" to diff against

    # Poll B (+5 min): still Hall, confirmed now (2nd consecutive raw match), one
    # new point in a new cell -> 5 min attributed.
    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
    ))
    assert coord._confirmed_room[duid] == "Hall"
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(300.0)

    # Poll C (+5 min 6 s): mop wash starts (transit=True) — but the trajectory
    # keeps growing (the robot is still physically moving toward the dock),
    # with TWO new points that would land in two brand-new cells if counted.
    # This is the actual guard under test: `_attribute_points` marks new
    # points "seen" regardless of `transit` (so they can never be replayed
    # later either), but only accrues cells/elapsed when `not transit` — a
    # weaker test that fed no new points during this poll would pass even if
    # that gate were deleted, since there'd be nothing to (mis)attribute.
    before_cells = {k: dict(v) for k, v in coord._room_cells[duid].items()}
    clock.advance(minutes=5, seconds=6)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall", transit=True, vacuuming=False,
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100},
            {"x": 700, "y": 100}, {"x": 100, "y": 400},
        ],
    ))
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(300.0)  # unchanged
    assert coord._room_cells[duid] == before_cells  # unchanged despite 2 new points
    assert coord._path_seen[duid]["dry"] == 4  # ...which are gone for good, not deferred
    assert coord.hass.bus.names() == ["anyvac_clean_started"]  # no room_done during transit
    assert coord._confirmed_room[duid] == "Hall"  # not reset by the transit poll either

    # Poll D (+5 min): mop wash ends, resumes cleaning Hall with one genuinely
    # new point (the two from poll C are gone — see `_path_seen` above).
    # Delta is measured from poll C's timestamp, so it's exactly 5 min — the
    # mop-wash gap itself was never "pending", it was dropped outright at C.
    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100},
            {"x": 700, "y": 100}, {"x": 100, "y": 400}, {"x": 900, "y": 700},
        ],
    ))
    assert coord._room_elapsed[duid]["Hall"] == pytest.approx(600.0)
    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 3

    # Poll E (+5 min): docks. Evidence (3 cells) clears the floor -> room_done +
    # history stamp fire; session end calibrates exactly 10 minutes (not the 20
    # minutes of wall-clock session length, which included the mop-wash gap).
    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))
    assert coord.hass.bus.names() == [
        "anyvac_clean_started", "anyvac_room_done", "anyvac_clean_finished",
    ]
    finished = coord.hass.bus.events[-1][1]
    assert finished["duration_min"] == 20  # wall-clock session length, includes the wash
    assert finished["calibrated"] == {"Hall": {"dry": {"before": None, "after": 10}}}
    assert coord.rooms_estimate[duid]["Hall"]["dry"] == 10


def test_transit_drive_through_not_counted_as_completed(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A robot whose dock sits in (or whose path merely crosses) a room it never
    actually cleans this session must not have that room calibrated — even
    though `_attribute_points` picks up a few cells there just from the
    trajectory passing through, evidence-gated by `completed` (firmware
    `cleaned_rooms` OR the debounced room-confirmation), never by cell count
    alone. Docs/13 A2/B8: this is the guard field-confirmed via the
    "not completed (transit only?)" rejection reason."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d2"

    # The robot is cleaning Bathroom the entire session — `vacuum_room_name`
    # never once reports "Hall", so Hall can never pass the 2-consecutive-poll
    # confirmation debounce. Its cells nonetheless accrue because a few path
    # points geometrically land inside Hall's bbox along the way.
    _poll(coord, _device(duid, vacuum_room_name="Bathroom", _path_dry=[{"x": 2100, "y": 100}]))

    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[{"x": 2100, "y": 100}, {"x": 2400, "y": 100}, {"x": 100, "y": 100}],
    ))
    assert coord._confirmed_room[duid] == "Bathroom"

    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[
            {"x": 2100, "y": 100}, {"x": 2400, "y": 100}, {"x": 100, "y": 100},
            {"x": 400, "y": 100}, {"x": 700, "y": 100}, {"x": 2700, "y": 100},
        ],
    ))
    assert len(coord._room_cells[duid]["Hall"]["dry"]) == 3  # clears the bare evidence floor

    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    rooms = coord._last_calib[duid]["rooms"]
    assert rooms["Bathroom"]["dry"]["accepted"] is True
    assert rooms["Hall"]["dry"]["cells"] == 3  # evidence existed...
    assert rooms["Hall"]["dry"]["accepted"] is False
    assert rooms["Hall"]["dry"]["reason"] == "not completed (transit only?)"  # ...but rejected
    assert "Hall" not in coord.rooms_estimate.get(duid, {})
    assert coord.rooms_estimate[duid]["Bathroom"]["dry"] > 0
    # No room_done ever fires for Hall either — it never got confirmed as the
    # robot's current room in the first place.
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
    duid = "d3"

    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
    ))
    assert coord._confirmed_room[duid] == "Hall"

    # Robot moves on to Bathroom; new points now land there instead of Hall.
    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
            {"x": 2100, "y": 100},
        ],
    ))
    # Bathroom not yet confirmed (only 1 consecutive poll) — Hall's room_done
    # hasn't fired yet either.
    assert coord._confirmed_room[duid] == "Hall"

    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
            {"x": 2100, "y": 100}, {"x": 2400, "y": 100}, {"x": 2700, "y": 100},
        ],
    ))
    # Bathroom now confirmed (2nd consecutive) -> Hall's room_done fires ("left").
    assert coord._confirmed_room[duid] == "Bathroom"
    assert coord.hass.bus.names() == ["anyvac_clean_started", "anyvac_room_done"]
    assert coord.hass.bus.events[-1][1]["room"] == "Hall"

    clock.advance(minutes=5)
    _poll(coord, _device(duid, in_cleaning=False))

    finished = coord.hass.bus.events[-1][1]
    assert sorted(finished["rooms"]) == ["Bathroom", "Hall"]
    assert finished["calibrated"] == {
        "Hall": {"dry": {"before": None, "after": 5}},
        "Bathroom": {"dry": {"before": None, "after": 10}},
    }
    assert coord.rooms_estimate[duid]["Hall"]["dry"] == 5
    assert coord.rooms_estimate[duid]["Bathroom"]["dry"] == 10
    # A second `anyvac_room_done` for Bathroom fires on docking.
    room_done_rooms = [e["room"] for name, e in coord.hass.bus.events if name == "anyvac_room_done"]
    assert room_done_rooms == ["Hall", "Bathroom"]


def test_plan_scope_transit_labeling(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    """Docs/17 §1.3: a room outside the CURRENT JOB's scope must be labeled
    transit at attribution time — a finer, earlier guard than the
    `completed`-at-calibration check `test_transit_drive_through_...` covers.
    Crucially this must catch drive-through even when the vacuum's raw state
    is NOT a TRANSIT_STATE at all (`transit=False`, genuinely "cleaning" per
    the firmware) — that's exactly the case the state-only gate cannot see,
    since `_job_rooms` is a knowledge only an orchestrated `anyvac.clean` job
    provides (docs/14 §5), not something derivable from vacuum state."""
    coord = _new_coordinator(monkeypatch, clock)
    duid = "d4"

    # Job scope: only Bathroom belongs to this job — Hall is a room the robot
    # happens to physically cross without it being part of the plan at all.
    coord.set_job_rooms(duid, {"Bathroom"})

    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[{"x": 2100, "y": 100}, {"x": 100, "y": 100}],
    ))
    assert coord._room_cells[duid].get("Hall") is None  # never touched
    assert "Bathroom" in coord._room_cells[duid]
    assert len(coord._transit_cells[duid]["Hall"]) == 1

    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[
            {"x": 2100, "y": 100}, {"x": 100, "y": 100},
            {"x": 2400, "y": 100}, {"x": 400, "y": 100},
        ],
    ))
    # Elapsed time went entirely to Bathroom — Hall's out-of-scope point
    # contributed zero weight, it didn't just get excluded from cells.
    assert coord._room_elapsed[duid] == {"Bathroom": pytest.approx(300.0)}
    assert coord._room_cells[duid].get("Hall") is None
    assert len(coord._transit_cells[duid]["Hall"]) == 2

    # Once the job's scope is cleared (mirrors _JobRunner.finish()), the SAME
    # room reverts to normal state-only gating — a later manual/native run
    # through Hall is attributed normally again, not permanently blacklisted.
    coord.set_job_rooms(duid, None)
    clock.advance(minutes=5)
    _poll(coord, _device(
        duid, vacuum_room_name="Bathroom",
        _path_dry=[
            {"x": 2100, "y": 100}, {"x": 100, "y": 100}, {"x": 2400, "y": 100},
            {"x": 400, "y": 100}, {"x": 700, "y": 100},
        ],
    ))
    assert "Hall" in coord._room_cells[duid]
    assert len(coord._transit_cells[duid]["Hall"]) == 2  # unchanged, no new out-of-scope points


def test_two_households_share_one_coordinator(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Two independently-owned vacuums (two Roborock accounts/homes, both added
    to the same HA instance -> one shared AnyVacCoordinator, docs/14) must not
    cross-pollute PER-DUID state even when they coincidentally name a room the
    same thing ("Hall" in both homes here). Learned time estimates, sessions,
    and coverage cells are all keyed by duid and must stay isolated.

    `_history` (last-cleaned timestamps) is the one deliberate exception —
    it's documented as "aggregates across all vacuums" (keyed by room NAME
    only, for the same-household multi-robot case) — so this test also pins
    down that a genuine cross-household name collision DOES merge the
    last-cleaned stamp. That's a known, currently-accepted tradeoff, not a
    bug this test is asserting should be fixed; it exists so a future change
    to `_history`'s keying is a deliberate decision, not a silent regression
    either way."""
    coord = _new_coordinator(monkeypatch, clock)

    # Household A: duid "home-a", a fast robot, 10-minute Hall clean.
    _poll(coord, _device(
        "home-a", vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(
        "home-a", vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
    ))
    clock.advance(minutes=1)
    _poll(coord, _device("home-a", in_cleaning=False))

    # Household B: duid "home-b", also has a room called "Hall" (unrelated
    # physical home, coincidental name) — a slower robot, longer session.
    clock.advance(minutes=1)
    _poll(coord, _device(
        "home-b", vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(
        "home-b", vacuum_room_name="Hall",
        _path_dry=[{"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100}],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(
        "home-b", vacuum_room_name="Hall",
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
            {"x": 100, "y": 400},
        ],
    ))
    clock.advance(minutes=5)
    _poll(coord, _device(
        "home-b", vacuum_room_name="Hall",
        _path_dry=[
            {"x": 100, "y": 100}, {"x": 400, "y": 100}, {"x": 700, "y": 100},
            {"x": 100, "y": 400}, {"x": 400, "y": 400},
        ],
    ))
    clock.advance(minutes=1)
    _poll(coord, _device("home-b", in_cleaning=False))

    # Per-duid learned estimates stayed independent despite the identical room name.
    assert coord.rooms_estimate["home-a"]["Hall"]["dry"] == 5
    assert coord.rooms_estimate["home-b"]["Hall"]["dry"] == 15
    # Session/tracking dicts never leaked across duids either (both cleared to
    # empty at their own session end, independently).
    assert coord._session_rooms["home-a"] == set()
    assert coord._session_rooms["home-b"] == set()
    assert coord._room_cells["home-a"] == {}
    assert coord._room_cells["home-b"] == {}

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

    # Coverage/calibration state is a separate, already-validated system
    # (docs/16) — `_room_elapsed` must keep resetting per sortie exactly as
    # before (it's zeroed at session start, then re-earns time from THIS
    # poll's own delta only); docs/27 only changes the visual trace's
    # lifetime. A non-reset would show accumulated time from both sorties
    # merged into one continuous span instead of sortie 2 starting fresh.
    assert coord._session_start[duid] == clock.now


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
