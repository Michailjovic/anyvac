"""docs/49 — the KIND of a run (dry / wet / both) and how it is announced.

Evidence (the run's real footprint) decides; until it exists the job's plan says
what was ordered; the mode knob comes last and only as captured during active
cleaning. The finish poll's knob is never read: the S7 MaxV firmware resets the
water mode in the very state update that starts `returning_home`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import custom_components.anyvac.coordinator as coordinator_mod
from tests.test_coordinator_pipeline import HALL, _Clock, _device, _new_coordinator, _poll

ACTIVE = dict(status_state="segment_cleaning")
RETURNING = dict(status_state="returning_home", transit=True)
DOCKED = dict(in_cleaning=False, vacuuming=False, transit=False, status_state="charging")


def _rig(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock(datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    coord = _new_coordinator(monkeypatch, clock)
    coord._signal_candidate = {}
    coord._last_run = {}
    coord._job_state = None
    return coord, clock


def _step(coord, clock, **kw):
    _poll(coord, _device("s7", **kw))
    clock.advance(seconds=30)


def _events(coord, name):
    return [d for e, d in coord.hass.bus.events if e.endswith(f"_{name}")]


def _run(coord, clock, polls, *, tail=None):
    _step(coord, clock, in_cleaning=False, vacuuming=False, clean_type="wet")
    for kw in polls:
        _step(coord, clock, **kw)
    # The firmware keeps the finished run's trajectory in the map until the next
    # clean, so the closing polls carry the same paths (an empty list would read
    # as a path reset).
    paths = {k: v for k, v in polls[-1].items() if k.startswith("_path")} if polls else {}
    for kw in tail or [dict(RETURNING, clean_type="wet"), dict(DOCKED, clean_type="wet")]:
        _step(coord, clock, **{**paths, **kw})


def test_s1_room_known_dry_run(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _run(coord, clock, [dict(clean_type="dry", vacuum_room_name="Hall", _path_dry=HALL, **ACTIVE)] * 2)
    done = _events(coord, "run_finished")[-1]
    assert done["clean_type"] == "dry"
    assert done["kinds"] == ["dry"]


def test_s2_no_room_name_during_the_run(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _run(coord, clock, [dict(clean_type="dry", _path_dry=HALL, **ACTIVE)] * 3)
    assert _events(coord, "run_finished")[-1]["clean_type"] == "dry"
    assert _events(coord, "clean_finished")[-1]["clean_type"] == "dry"


def test_s3_in_cleaning_holds_while_the_water_mode_is_already_reset(monkeypatch):
    """The field case (S7 MaxV, 2026-11-13): mode `off` all run long, `mild` in the
    same update as `returning_home` — with `in_cleaning` still set."""
    coord, clock = _rig(monkeypatch)
    _run(
        coord, clock,
        [dict(clean_type="dry", vacuum_room_name="Hall", _path_dry=HALL, **ACTIVE)] * 3,
        tail=[
            dict(RETURNING, clean_type="wet", vacuum_room_name="Hall", _path_dry=HALL),
            dict(RETURNING, clean_type="wet", _path_dry=HALL),
            dict(DOCKED, clean_type="wet"),
        ],
    )
    done = _events(coord, "run_finished")[-1]
    assert done["clean_type"] == "dry"


def test_s4_stale_knob_on_the_first_poll_is_not_captured(monkeypatch):
    coord, clock = _rig(monkeypatch)
    polls = [dict(clean_type="wet", **ACTIVE)] + [dict(clean_type="dry", _path_dry=HALL, **ACTIVE)] * 2
    _run(coord, clock, polls)
    assert _events(coord, "run_finished")[-1]["clean_type"] == "dry"


def test_knob_is_not_captured_in_transit_or_idle(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _step(coord, clock, clean_type="wet", **dict(RETURNING))
    _step(coord, clock, clean_type="wet", **dict(RETURNING))
    assert coord._session_clean_type == {}


def test_both_footprints_make_a_both_run(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _run(
        coord, clock,
        [dict(clean_type="wet", vacuum_room_name="Hall", _path_dry=HALL, _path_wet=HALL, **ACTIVE)] * 2,
    )
    done = _events(coord, "run_finished")[-1]
    assert done["clean_type"] == "both"
    assert done["clean_type_source"] == "evidence"
    assert done["kinds"] == ["dry", "wet"]
    assert done["rooms_kinds"] == {"Hall": ["dry", "wet"]}


def test_plan_decides_before_any_evidence(monkeypatch):
    coord, clock = _rig(monkeypatch)
    coord._job_state = {"queues": {"s7": {"items": [{"id": "dry-s7-0", "room": "Hall", "rooms": ["Hall"], "kind": "dry", "state": "queued"}]}}}
    _step(coord, clock, in_cleaning=False, vacuuming=False, clean_type="wet")
    _step(coord, clock, clean_type="wet", **ACTIVE)  # stale/odd knob, no footprint yet
    assert coord.resolve_run_kind("s7") == ("dry", "plan")
    assert _events(coord, "clean_started")[-1]["clean_type"] == "dry"
    assert _events(coord, "clean_started")[-1]["clean_type_source"] == "plan"


def test_signal_is_the_last_resort(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _step(coord, clock, in_cleaning=False, vacuuming=False, clean_type="dry")
    assert coord.resolve_run_kind("s7") == (None, None)
    _step(coord, clock, clean_type="dry", **ACTIVE)
    _step(coord, clock, clean_type="dry", **ACTIVE)
    assert coord.resolve_run_kind("s7") == ("dry", "signal")


def test_last_run_is_recorded_when_the_run_closes(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _run(coord, clock, [dict(clean_type="dry", vacuum_room_name="Hall", _path_dry=HALL, **ACTIVE)] * 2)
    rec = coord._last_run["s7"]
    assert rec["kind"] == "dry" and rec["source"] == "evidence" and rec["rooms"] == ["Hall"]
    assert rec["finished_at"]


def test_room_done_carries_the_kind(monkeypatch):
    coord, clock = _rig(monkeypatch)
    _run(coord, clock, [dict(clean_type="dry", vacuum_room_name="Hall", _path_dry=HALL, **ACTIVE)] * 2,
         tail=[dict(DOCKED, clean_type="wet")])
    done = _events(coord, "room_done")
    assert done and done[-1]["kind"] == "dry"


def test_status_names_read_by_the_coordinator_exist_in_python_roborock():
    """P1 (docs/49): `is_water_box_carriage_attached` was read for months and never
    existed. Every name `_extract_device` reads through `_s("…")` must exist on the
    real status trait."""
    pytest.importorskip("roborock")
    from roborock.devices.traits.v1.status import StatusTrait

    src = Path(coordinator_mod.__file__).read_text()
    names = set(re.findall(r'\b_s\("([a-z_0-9]+)"\)', src))
    assert "water_box_carriage_status" in names
    missing = sorted(n for n in names if not hasattr(StatusTrait, n))
    assert not missing, f"not on python-roborock's StatusTrait: {missing}"


def test_job_warning_is_exposed_in_job_progress(monkeypatch):
    coord, _clock = _rig(monkeypatch)
    coord.async_update_listeners = lambda: None
    coord.data = {}
    coord.set_job_state({
        "job_id": "j1", "started_at": _clock.now, "mode": "dry",
        "queues": {"s7": {"vacuum": "vacuum.s7", "items": [
            {"id": "dry-s7-0", "room": "Hall", "rooms": ["Hall"], "kind": "dry", "state": "queued", "est_min": 5},
        ]}},
    })
    coord.add_job_warning("select.s7_maxv_mop_intensity is 'mild', expected 'off'")
    coord.add_job_warning("select.s7_maxv_mop_intensity is 'mild', expected 'off'")
    assert coord.job_progress["warnings"] == ["select.s7_maxv_mop_intensity is 'mild', expected 'off'"]


def _runner(monkeypatch, states_get, warned, call=None, data=None):
    import custom_components.anyvac.jobrunner as jr
    from types import SimpleNamespace

    class _Coord:
        def __init__(self):
            self.data = data or {}

        def add_job_warning(self, text):
            warned.append(text)

    async def _default_call(domain, service, payload, blocking=False, context=None):
        return None

    async def _no_sleep(_):
        return None

    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=call or _default_call),
        states=SimpleNamespace(get=states_get),
    )
    runner = jr.JobRunner(hass, [])
    monkeypatch.setattr(jr, "_coordinators", lambda h: [_Coord()])
    monkeypatch.setattr(jr.asyncio, "sleep", _no_sleep)
    return runner


def test_select_that_did_not_take_is_reported_not_silent(monkeypatch):
    """A5 (docs/49): `mop_intensity: off` that does not take makes the "dry" robot
    mop. Never silent, never blocking."""
    from types import SimpleNamespace

    it = {"vacuum": "vacuum.s7", "selects": [{"entity_id": "select.s7_mop", "option": "off"}], "expect": {}}
    runner = _runner(monkeypatch, lambda eid: SimpleNamespace(state="mild"), [])
    assert runner._settings_mismatch(it, {}) == ["select.s7_mop is 'mild', expected 'off'"]
    runner = _runner(monkeypatch, lambda eid: SimpleNamespace(state="off"), [])
    assert runner._settings_mismatch(it, {}) == []
    # The fan the robot reports (`mop_signal`) is checked too.
    it = {"vacuum": "vacuum.s7", "selects": [], "expect": {"fan_speed": "max_plus"}}
    assert runner._settings_mismatch(it, {"mop_signal": {"fan_speed_name": "balanced"}}) == [
        "vacuum.s7 fan is 'balanced', expected 'max_plus'"
    ]


@pytest.mark.asyncio
async def test_select_of_a_robot_without_a_mop_is_not_a_job_warning(monkeypatch):
    """An S6 has a `mop_intensity` select that is always "unknown"; forcing it to
    `off` fails on every start. That is noise, not a problem of the run."""
    from types import SimpleNamespace

    async def _boom(domain, service, payload, blocking=False, context=None):
        raise RuntimeError("Error while calling SET_WATER_BOX_CUSTOM_MODE")

    it = {"vacuum": "vacuum.s6", "selects": [{"entity_id": "select.s6_mop_intensity", "option": "off"}]}
    runner = _runner(monkeypatch, lambda eid: SimpleNamespace(state="unknown"), [], call=_boom)
    assert await runner._apply_settings(it) == []
    runner = _runner(monkeypatch, lambda eid: SimpleNamespace(state="mild"), [], call=_boom)
    assert len(await runner._apply_settings(it)) == 1


@pytest.mark.asyncio
async def test_requested_passes_are_checked_only_while_the_item_runs(monkeypatch):
    """docs/52 O3: the passes the robot reports are only compared while THIS item
    runs (active state, its own segments). Right after the command the status
    still shows the previous task — that was the false "reports 1" warning."""
    from types import SimpleNamespace

    def dev(rep, state="segment_cleaning", segs=(17,)):
        return SimpleNamespace(data={"repeat": rep, "status_state": state, "target_segments": list(segs)})

    def item():
        return {"id": "i", "vacuum": "vacuum.s6", "segments": [17], "repeat": 2, "state": "active",
                "selects": [], "expect": {}}

    warned: list[str] = []
    runner = _runner(monkeypatch, lambda eid: None, warned, data={"d": dev(1)})
    await runner._reapply_and_verify("d", item())
    assert warned == ["vacuum.s6: asked for 2 passes, the robot reports 1"]

    warned.clear()
    for d in (dev(2), dev(1, state="returning_home"), dev(1, segs=(18,)), dev(None)):
        runner = _runner(monkeypatch, lambda eid: None, warned, data={"d": d})
        await runner._reapply_and_verify("d", item())
    assert warned == []


def test_legacy_path_is_only_decimated_when_it_is_exposed():
    """D4 (1.52.3): `path` / `mop_path` feed only the opt-in legacy mm attributes —
    with those off the RDP pass over them was wasted work on the event loop."""
    from types import SimpleNamespace

    # A wiggly line far longer than PATH_MAX_POINTS so decimation really shortens it.
    n = coordinator_mod.PATH_MAX_POINTS * 3
    pts = [SimpleNamespace(x=float(i * 40), y=float((i % 7) * 15 + (i % 3) * 9)) for i in range(n)]
    md = SimpleNamespace(
        vacuum_position=None, charger=None, rooms={}, cleaned_rooms=[], vacuum_room=None,
        vacuum_room_name=None, image=None,
        path=SimpleNamespace(path=[pts]), mop_path=SimpleNamespace(path=[pts]),
    )
    full = coordinator_mod._extract_map(md, decimate=True)
    lazy = coordinator_mod._extract_map(md, decimate=False)
    assert len(full["path"]) <= coordinator_mod.PATH_MAX_POINTS < n
    assert len(lazy["path"]) == len(lazy["mop_path"]) == n
    assert full["path_points"] == lazy["path_points"] == n
    assert lazy["_path_dry"] == full["_path_dry"]  # the live trace is unaffected


def test_dry_pass_with_water_still_running_is_reported(monkeypatch):
    it = {"vacuum": "vacuum.s7", "selects": [], "expect": {"water_off": True}}
    runner = _runner(monkeypatch, lambda eid: None, [])
    assert runner._settings_mismatch(it, {"mop_signal": {"water_mode_name": "mild"}}) == [
        "vacuum.s7 water is 'mild', expected 'off'"
    ]
    assert runner._settings_mismatch(it, {"mop_signal": {"water_mode_name": "off"}}) == []
