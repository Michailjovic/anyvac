"""Room-by-room job runner (docs/52 §3, §9.2) against a fake fleet that
behaves like the measured firmware (`_fake_robot`).

The file keeps its name from the docs/23 pool dispatch it replaced (a deleted
test file would linger on the user's disk)."""

from __future__ import annotations

import pytest

import custom_components.anyvac.jobrunner as jr
from tests._fake_robot import Rig, item

pytestmark = pytest.mark.asyncio


async def _start(rig: Rig, items, **kw) -> jr.JobRunner:
    runner = jr.JobRunner(rig.hass, items, **kw)
    await runner.start()
    await rig.drain()
    return runner


async def test_one_room_per_command_next_one_on_the_way_home(monkeypatch) -> None:
    """S1: a dry robot gets one room, the next as soon as it turns home."""
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    runner = await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Bathroom", 19),
        item("s6", "vacuum.s6", "dry", "Corridor", 20),
    ])
    assert rig.hass.services.sent("vacuum.s6") == [[19]]
    await rig.poll()  # the robot took it
    assert runner.queues["s6"]["items"][0]["state"] == "active"

    await rig.finish_room(s6, ["Bathroom"])
    # Sent on the way home, without waiting for the dock (docs/52 §2.8).
    assert rig.hass.services.sent("vacuum.s6") == [[19], [20]]
    assert s6.taken == [[19], [20]]
    await rig.poll()
    await rig.finish_room(s6, ["Corridor"])
    assert rig.hass.bus.names().count("anyvac_run_finished") == 1
    report = rig.hass.bus.of("anyvac_run_finished")[0]
    assert report["vacuum"] == "vacuum.s6" and report["kind"] == "dry"
    assert report["rooms"] == ["Bathroom", "Corridor"]
    assert [r["room"] for r in report["rooms_result"]] == ["Bathroom", "Corridor"]
    assert report["blocked"] == []
    assert rig.hass.bus.names().count("anyvac_job_finished") == 1
    assert rig.coord.job_rooms == {}  # the job released the robot
    assert rig.coord.saved[-1] is None


async def test_wet_room_waits_for_the_dry_pass_of_the_same_room(monkeypatch) -> None:
    """S3: the mop robot only gets a room once its dry pass is done."""
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    rig.add("s8", "vacuum.s8", wet=True)
    await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Corridor", 20),
        item("s8", "vacuum.s8", "wet", "Corridor", 18),
    ])
    assert rig.hass.services.sent("vacuum.s8") == []
    await rig.poll()
    await rig.finish_room(s6, ["Corridor"])
    assert rig.hass.services.sent("vacuum.s8") == [[18]]


async def test_by_room_washing_waits_for_the_dock(monkeypatch) -> None:
    """S5: washing after every room — the robot docks and washes before the
    next wet room; the next one goes out only once it is docked."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s8", "vacuum.s8", "wet", "Bathroom", 17),
        item("s8", "vacuum.s8", "wet", "Corridor", 18),
    ], wash={"s8": {"mode": "room", "interval_min": 20}})
    await rig.poll()
    await rig.finish_room(s8, ["Bathroom"])
    assert rig.hass.services.sent("vacuum.s8") == [[17]]
    assert runner.queues["s8"]["phase"] == "washing"
    await rig.dock(s8, washed=True)
    assert rig.hass.services.sent("vacuum.s8") == [[17], [18]]


async def test_by_time_washing_chains_until_the_interval(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s8", "vacuum.s8", "wet", "A", 1),
        item("s8", "vacuum.s8", "wet", "B", 2),
        item("s8", "vacuum.s8", "wet", "C", 3),
    ], wash={"s8": {"mode": "time", "interval_min": 8, "mop_min_since_wash": 0.0}})
    await rig.poll()
    await rig.finish_room(s8, ["A"])  # 5 min mopped < 8
    assert rig.hass.services.sent("vacuum.s8") == [[1], [2]]
    await rig.poll()
    await rig.finish_room(s8, ["B"])  # 10 min >= 8: dock and wash first
    assert rig.hass.services.sent("vacuum.s8") == [[1], [2]]
    await rig.dock(s8, washed=True)
    assert rig.hass.services.sent("vacuum.s8") == [[1], [2], [3]]
    assert runner.queues["s8"]["wash"]["mop_min_since_wash"] == 0.0


async def test_settings_are_sent_again_after_the_robot_took_the_task(monkeypatch) -> None:
    """S8 resets fan/mop mode when it takes a task (docs/52 §8.1)."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    sel = [{"entity_id": "select.s8_mop_mode", "option": "deep_plus"}]
    await _start(rig, [item("s8", "vacuum.s8", "wet", "A", 1, fan_speed="off", selects=sel)])
    assert rig.hass.services.count("vacuum", "set_fan_speed") == 1
    assert s8.data["mop_signal"]["fan_speed_name"] == "balanced"  # reset by the firmware
    await rig.poll()
    assert rig.hass.services.count("vacuum", "set_fan_speed") >= 2
    assert rig.hass.services.count("select", "select_option") >= 2
    assert s8.data["mop_signal"]["fan_speed_name"] == "off"
    assert rig.coord.warnings == []


async def test_command_the_robot_ignores_is_sent_once_more_then_blocked(monkeypatch) -> None:
    """S12: the firmware drops the command (the service still succeeds)."""
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    runner = await _start(rig, [item("s6", "vacuum.s6", "dry", "A", 1), item("s6", "vacuum.s6", "dry", "B", 2)])
    # The robot did not move (e.g. cleaning something else): undo the take.
    s6.data.update(in_cleaning=False, status_state="charging", target_segments=[])
    rig.clock.advance(seconds=50)
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s6") == [[1], [1]]
    s6.data.update(in_cleaning=False, status_state="charging", target_segments=[])
    rig.clock.advance(seconds=50)
    await rig.poll()
    first = runner.queues["s6"]["items"][0]
    assert first["state"] == "blocked" and first["reason"] == "command not accepted"
    assert rig.hass.services.sent("vacuum.s6")[-1] == [2]  # goes on with the next


async def test_a_room_the_robot_did_not_clean_is_blocked_with_a_reason(monkeypatch) -> None:
    """S17: the robot ended the task without cleaning the room (water ran out)."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [item("s8", "vacuum.s8", "wet", "Living", 16)])
    await rig.poll()
    await rig.finish_room(s8, ["Living"], cleaned=False, water_shortage=True)
    it = runner.queues["s8"]["items"][0]
    assert it["state"] == "blocked" and it["reason"] == "water_shortage"
    assert rig.hass.bus.of("anyvac_run_finished")[0]["blocked"] == [
        {"room": "Living", "kind": "wet", "reason": "water_shortage"}
    ]


async def test_low_battery_robot_waits_in_the_dock(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6", battery=22)
    runner = await _start(rig, [item("s6", "vacuum.s6", "dry", "A", 1)])
    assert rig.hass.services.sent() == []
    assert runner.queues["s6"]["phase"] == "charging"
    s6.data["battery"] = 35
    await rig.poll()
    assert rig.hass.services.sent() == [[1]]


async def test_batch_robot_gets_all_rooms_in_one_command(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    batch = item("s6", "vacuum.s6", "dry", "A", 1, batch=True)
    batch.update(rooms=["A", "B"], segments=[1, 2], room=None)
    await _start(rig, [batch])
    assert rig.hass.services.sent() == [[1, 2]]
    await rig.poll()
    await rig.finish_room(s6, ["A", "B"])
    assert rig.hass.bus.of("anyvac_run_finished")[0]["rooms"] == ["A", "B"]


async def test_never_two_robots_in_one_room(monkeypatch) -> None:
    """docs/53 P4, field 2026-11-25: Corridor's dry pass was removed and added
    again; in between the wet gate opened and S8 took Corridor. The re-added
    dry pass must wait until S8 is out of the room."""
    rig = Rig(monkeypatch)
    rig.add("s7", "vacuum.s7")
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s7", "vacuum.s7", "dry", "Corridor", 17),
        item("s8", "vacuum.s8", "wet", "Corridor", 18),
    ])
    runner.remove_room("Corridor", "dry")  # S7 had it: pause, nothing left -> home
    await rig.drain()
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s8") == [[18]]  # gate open, S8 goes
    runner.add_items([item("s7", "vacuum.s7", "dry", "Corridor", 17)])
    rig.hass.robots["s7"].data.update(in_cleaning=False, status_state="charging", target_segments=[])
    await rig.drain()
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s7") == [[17]]  # only the first one
    await rig.finish_room(s8, ["Corridor"])
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s7") == [[17], [17]]
