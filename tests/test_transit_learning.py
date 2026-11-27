"""docs/53 §3: the coordinator measures travel from the command to the first
snapshot of the robot cleaning its target room."""
from __future__ import annotations

from datetime import timedelta

import pytest
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


def test_a_start_from_the_dock_carries_the_robots_measured_start_time() -> None:
    from custom_components.anyvac import transit as tr

    default = {"dock": {"Bathroom": 0.3, "Kitchen": 0.2}, "Kitchen": {"Bathroom": 0.25}}
    learned = {"dock": {"Bathroom": 1.1}}
    assert tr.dock_start(default, learned) == pytest.approx(0.8)
    out = tr.blend(default, learned)
    assert out["dock"] == {"Bathroom": 1.1, "Kitchen": pytest.approx(1.0)}
    assert out["Kitchen"] == {"Bathroom": 0.25}
    assert tr.dock_start(default, {}) == 0.0
    assert tr.dock_start(default, {"dock": {"Bathroom": 9.0}}) == tr.DOCK_START_MAX


def test_wash_lengths_are_learned_per_robot(monkeypatch) -> None:
    """Field 2026-11-27: the S8 pre-wash took 5.2 min; a wash inside a task is
    measured from going to the dock to back at work."""
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = object.__new__(c.AnyVacCoordinator)
    co._wash_watch, co._wash_learned, co._wash_store = {}, {}, None
    co._delay_save = lambda *a, **k: None
    monkeypatch.setattr(c.dt_util, "utcnow", lambda: now[0])

    def at(sec, state, cleaning):
        now[0] = t0 + timedelta(seconds=sec)
        co._note_wash(SimpleNamespace(duid="s8", data={"status_state": state, "in_cleaning": cleaning}))

    at(0, "charging", False)
    at(10, "washing_the_mop", False)          # pre-wash in the dock
    at(200, "washing_the_mop", False)
    at(322, "charging", False)
    at(1000, "segment_cleaning", True)
    at(1030, "going_to_wash_the_mop", True)   # wash inside the task
    at(1060, "washing_the_mop", True)
    at(1300, "segment_cleaning", True)
    assert co.wash_minutes("s8") == {"dock": 5.2, "mid": 4.5}
    at(1400, "washing_the_mop", False)
    at(1410, "charging", False)               # 10 s: noise, not a wash
    assert co.wash_minutes("s8") == {"dock": 5.2, "mid": 4.5}


def test_the_timing_model_uses_the_learned_wash_lengths() -> None:
    from custom_components.anyvac.planner import simulate_queues

    def r(**extra):
        return {"wash_mode": "time", "interval": 10, "items": [
            {"id": "w", "kind": "wet", "rooms": ["A"], "est_min": 15}], **extra}
    base = simulate_queues({"s8": r()})["w"]
    learned = simulate_queues({"s8": r(wash_dock=5.0, wash_mid=4.0)})["w"]
    assert learned[0] == pytest.approx(base[0] + 5.0 - 3.0)       # pre-wash
    assert (learned[1] - learned[0]) == pytest.approx((base[1] - base[0]) + 4.0 - 2.25)  # one wash in the task


def test_one_slow_transit_does_not_set_the_time(monkeypatch) -> None:
    """Field 2026-11-27: S8 met S6 in the kitchen on its way — 1.4 min instead
    of ~0.4. The kept samples' low median ignores a single slow one."""
    from custom_components.anyvac import transit as tr

    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _coord(monkeypatch, now)

    def drive(seconds):
        co._last_task_room["s8"] = "Bedroom"
        co.set_task_item("s8", {"Corridor"})
        now[0] = now[0] + timedelta(seconds=seconds)
        co._note_transit(_dev("Corridor"))

    drive(82)                                 # the slow one comes first
    assert co._transit_learned["s8"]["Bedroom"]["Corridor"] == pytest.approx(1.37)
    drive(24)
    assert co._transit_learned["s8"]["Bedroom"]["Corridor"] == pytest.approx(0.4)
    drive(26)
    drive(90)                                 # another encounter
    assert co._transit_learned["s8"]["Bedroom"]["Corridor"] == pytest.approx(0.43)
    for _ in range(5):
        drive(25)
    assert co._transit_samples["s8"]["Bedroom"]["Corridor"] == [0.42] * 5
    assert tr.low_median([1.0, 3.0]) == 1.0
