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
    coord._job_state = {"passes": [{"duid": "s7", "room": "Hall", "kind": "dry", "done_at": None}]}
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
    coord.set_job_plan([{"room": "Hall", "kind": "dry", "duid": "s7", "est_min": 5, "finish_min": 5}], 5)
    coord.add_job_warning("select.s7_maxv_mop_intensity is 'mild', expected 'off'")
    coord.add_job_warning("select.s7_maxv_mop_intensity is 'mild', expected 'off'")
    assert coord.job_progress["warnings"] == ["select.s7_maxv_mop_intensity is 'mild', expected 'off'"]


@pytest.mark.asyncio
async def test_select_that_did_not_take_is_reported_not_silent(monkeypatch):
    """A5 (docs/49): `mop_intensity: off` that does not take makes the "dry" robot
    mop. Never silent, never blocking."""
    import custom_components.anyvac.services as services_mod
    from types import SimpleNamespace

    warned: list[str] = []

    class _Coord:
        def add_job_warning(self, text):
            warned.append(text)

    async def _call(domain, service, data, blocking=False, context=None):
        return None

    async def _no_sleep(_):
        return None

    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=_call),
        states=SimpleNamespace(get=lambda eid: SimpleNamespace(state="mild")),
    )
    runner = object.__new__(services_mod._JobRunner)
    runner.hass, runner.context = hass, None
    monkeypatch.setattr(services_mod, "_coordinators", lambda h: [_Coord()])
    monkeypatch.setattr(services_mod.asyncio, "sleep", _no_sleep)
    await runner._apply_selects([{"entity_id": "select.s7_mop", "option": "off"}])
    assert warned == ["select.s7_mop is 'mild', expected 'off'"]

    warned.clear()
    hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state="off"))
    await runner._apply_selects([{"entity_id": "select.s7_mop", "option": "off"}])
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
