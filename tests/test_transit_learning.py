"""docs/53 §3: the coordinator measures travel from the command to the first
snapshot of the robot cleaning its target room."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from custom_components.anyvac import coordinator as c


def _coord(monkeypatch, now):
    co = object.__new__(c.AnyVacCoordinator)
    co._transit_watch, co._transit_learned, co._transit_default = {}, {}, {}
    co._transit_store = None
    co._delay_save = lambda *a, **k: None
    co._geo = {}
    co._task_item, co._last_task_room = {}, {}
    co.data = {"s8": SimpleNamespace(data={"in_cleaning": True, "status_state": "returning_home"})}
    monkeypatch.setattr(c.dt_util, "utcnow", lambda: now[0])
    return co


def _dev(room, state="segment_cleaning"):
    return SimpleNamespace(duid="s8", data={"status_state": state, "vacuum_room_name": room})


def test_learns_room_to_room_travel(monkeypatch) -> None:
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _coord(monkeypatch, now)
    co._last_task_room["s8"] = "Kitchen"
    co.set_task_item("s8", {"Living"})
    now[0] = t0 + timedelta(seconds=30)
    co._note_transit(_dev("Hall"))           # on its way through the hall
    now[0] = t0 + timedelta(seconds=72)
    co._note_transit(_dev("Living"))
    assert co._transit_learned == {"s8": {"Kitchen": {"Living": 1.2}}}
    assert co.transit_minutes("s8") == {"Kitchen": {"Living": 1.2}}


def test_a_wash_on_the_way_is_not_travel(monkeypatch) -> None:
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _coord(monkeypatch, now)
    co.data["s8"].data.update(in_cleaning=False, status_state="charging")
    co.set_task_item("s8", {"Living"})
    assert co._transit_watch["s8"]["from"] == "dock"
    co._note_transit(_dev("Kitchen", "washing_the_mop"))
    now[0] = t0 + timedelta(minutes=4)
    co._note_transit(_dev("Living"))
    assert co._transit_learned == {}


def test_the_end_of_a_task_remembers_where_the_robot_is() -> None:
    co = object.__new__(c.AnyVacCoordinator)
    co._task_item, co._last_task_room, co._transit_watch = {"s8": {"Bath"}}, {}, {}
    co.data = {"s8": SimpleNamespace(data={"in_cleaning": True, "status_state": "returning_home"})}
    co.set_task_item("s8", None)
    assert co.robot_place("s8") == "Bath"
    co.data["s8"].data.update(status_state="charging")
    assert co.robot_place("s8") == "dock"
