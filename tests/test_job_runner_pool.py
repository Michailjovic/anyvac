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


def _wet(room: str, seg: int) -> dict:
    it = item("s8", "vacuum.s8", "wet", room, seg, fan_speed="off",
              selects=[{"entity_id": "select.s8_mop_intensity", "option": "standard"}])
    it["watch"] = ["select.s8_mop_intensity", "select.s8_mop_mode"]
    return it


async def test_a_setting_changed_on_the_robot_is_kept_for_the_rest_of_the_queue(monkeypatch) -> None:
    """User 2026-11-25: presets set the start; what the user then changes on
    the robot (fan, mop intensity, mop mode) holds for the next rooms too."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    rig.hass.services.states["select.s8_mop_mode"] = "deep_plus"
    runner = await _start(rig, [_wet("A", 1), _wet("B", 2)])
    await rig.poll()  # took it: settings again, checked, baseline
    first, second = runner.queues["s8"]["items"]
    assert first.get("baseline")
    # The user turns the robot down in the app.
    s8.data["mop_signal"] = {"fan_speed_name": "quiet"}
    rig.hass.services.states["select.s8_mop_intensity"] = "mild"
    rig.hass.services.states["select.s8_mop_mode"] = "standard"
    await rig.poll()
    assert second["fan_speed"] == "quiet"
    assert {s["entity_id"]: s["option"] for s in second["selects"]} == {
        "select.s8_mop_intensity": "mild", "select.s8_mop_mode": "standard",
    }
    calls_before = len(rig.hass.services.calls)
    await rig.finish_room(s8, ["A"])
    sent = rig.hass.services.calls[calls_before:]
    assert ("vacuum", "set_fan_speed", {"entity_id": "vacuum.s8", "fan_speed": "quiet"}) in sent
    assert ("select", "select_option", {"entity_id": "select.s8_mop_intensity", "option": "mild"}) in sent
    assert rig.coord.warnings == []  # the next room checks against the new values


async def test_a_firmware_reset_while_returning_is_not_taken_as_a_change(monkeypatch) -> None:
    """S7 MaxV resets the water when a task ends (docs/52 §8.2) — not the user."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [_wet("A", 1), _wet("B", 2)])
    await rig.poll()
    second = runner.queues["s8"]["items"][1]
    s8.data["status_state"] = "returning_home"
    rig.hass.services.states["select.s8_mop_intensity"] = "mild"
    await rig.poll()
    assert {s["entity_id"]: s["option"] for s in second["selects"]} == {"select.s8_mop_intensity": "standard"}


async def test_edit_job_settings_change_the_queue_and_the_room_being_cleaned(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    runner = await _start(rig, [
        item("s6", "vacuum.s6", "dry", "A", 1, fan_speed="max"),
        item("s6", "vacuum.s6", "dry", "B", 2, fan_speed="max"),
    ])
    await rig.poll()
    fields = {"selects": [], "fan_speed": "quiet", "repeat": 1,
              "expect": {"fan_speed": "quiet", "mop_mode": None, "water_off": False}}
    assert len(runner.update_settings("s6", "dry", fields)) == 2
    await rig.drain()
    assert s6.data["mop_signal"]["fan_speed_name"] == "quiet"  # live, now
    assert runner.queues["s6"]["items"][1]["fan_speed"] == "quiet"


async def test_retry_puts_a_blocked_room_back_into_its_queue(monkeypatch) -> None:
    """The card's Retry (docs/52 §5 item 3): after the user filled the tank the
    blocked room goes out again like any queued one."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s8", "vacuum.s8", "wet", "Living", 16),
        item("s8", "vacuum.s8", "wet", "Hall", 17),
    ])
    await rig.poll()
    await rig.finish_room(s8, ["Living"], cleaned=False, water_shortage=True)
    living = runner.queues["s8"]["items"][0]
    assert living["state"] == "blocked"
    assert runner.retry("Living", "wet") == [living["id"]]
    assert living["state"] == "queued" and "reason" not in living and living["attempts"] == 0
    assert runner.retry("Living", "wet") == []  # nothing blocked any more
    assert rig.hass.services.sent("vacuum.s8") == [[16], [17]]  # S8 is on Hall now
    await rig.poll()
    await rig.finish_room(s8, ["Hall"])
    assert rig.hass.services.sent("vacuum.s8") == [[16], [17], [16]]  # Living again


async def test_a_chained_room_is_accepted_while_the_map_still_shows_the_last_one(monkeypatch) -> None:
    """Field 2026-11-26: S8 took Living room on its way home from Kitchen, but its
    map kept listing Kitchen's segments for 45 s — the room was blocked as "command
    not accepted", the job ended under the cleaning robot and the queue vanished."""
    rig = Rig(monkeypatch)
    s8 = rig.add("s8", "vacuum.s8")
    runner = await _start(rig, [item("s8", "vacuum.s8", "dry", "Kitchen", 3), item("s8", "vacuum.s8", "dry", "Living", 4)])
    await rig.poll()
    await rig.finish_room(s8, ["Kitchen"])
    assert rig.hass.services.sent("vacuum.s8")[-1] == [4]
    s8.data["target_segments"] = [3]  # the map lags behind the task
    for _ in range(5):
        rig.clock.advance(seconds=15)
        await rig.poll()
    living = runner.queues["s8"]["items"][1]
    assert living["state"] == "active"
    assert not runner._closed


async def test_a_room_blocked_as_not_accepted_that_gets_cleaned_counts_as_done(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    runner = await _start(rig, [item("s6", "vacuum.s6", "dry", "A", 1)])
    s6.data.update(target_segments=[9])  # a map that names some other room
    for _ in range(8):
        rig.clock.advance(seconds=15)
        await rig.poll()
    first = runner.queues["s6"]["items"][0]
    assert first["state"] == "blocked" and first["reason"] == "command not accepted"
    assert not runner._closed  # the robot is cleaning — the job waits
    await rig.finish_room(s6, ["A"])
    assert first["state"] == "done"


async def test_timer_callbacks_run_in_the_event_loop() -> None:
    """`async_call_later` runs a plain function in a worker thread."""
    from homeassistant.core import is_callback

    assert is_callback(jr.JobRunner._on_timeout)
    assert is_callback(jr.JobRunner._on_coordinator_update)


def _washes(rig, entity: str) -> int:
    return sum(
        1 for dom, svc, d in rig.hass.services.calls
        if dom == "vacuum" and svc == "send_command" and d.get("command") == "app_start_wash" and d["entity_id"] == entity
    )


async def test_wet_robot_washes_its_mop_before_the_dry_pass_ends(monkeypatch) -> None:
    """Field idea 2026-11-26: S8 waits in the dock for Kitchen's dry pass; it
    washes a few minutes before the room is ready, not after it."""
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Kitchen", 3, est=10.0),
        item("s8", "vacuum.s8", "wet", "Kitchen", 3, est=10.0),
    ], wash={"s8": {"mode": "room", "interval_min": 15}})
    await rig.poll()
    assert _washes(rig, "vacuum.s8") == 0  # ~10 min to go: too early
    s6.data["rooms_progress"] = {"Kitchen": {"dry_pct": 70}}  # ~3 min left
    await rig.poll()
    assert _washes(rig, "vacuum.s8") == 1
    assert runner.queues["s8"]["prewash_for"] == "wet-s8-Kitchen"
    await rig.poll()
    assert _washes(rig, "vacuum.s8") == 1  # once
    assert runner.queues["s8"]["wash"].get("washed_at")
    # Washed and not mopped since: the room starts without a wash first.
    from custom_components.anyvac.planner import live_robot_model
    assert live_robot_model(runner.queues["s8"], s8.data)["pre_wash"] is False
    # The dry pass ends while it still washes: it waits, then goes.
    await rig.finish_room(s6, ["Kitchen"])
    assert rig.hass.services.sent("vacuum.s8") == []
    s8.data["status_state"] = "charging"
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s8") == [[3]]


async def test_no_pre_wash_without_a_washing_dock_or_when_the_mop_is_clean(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    rig.add("s7", "vacuum.s7", wet=True)
    rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Kitchen", 3, est=2.0),
        item("s7", "vacuum.s7", "wet", "Kitchen", 3, est=10.0),
        item("s8", "vacuum.s8", "wet", "Kitchen", 3, est=10.0),
    ], wash={"s7": {"mode": None}, "s8": {"mode": "room", "interval_min": 15, "washed_at": "2026-11-25T17:59:00+00:00"}})
    await rig.poll()
    assert _washes(rig, "vacuum.s7") == 0
    assert _washes(rig, "vacuum.s8") == 0


async def test_no_pre_wash_when_the_dock_washed_the_mop_after_its_last_mopping(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    rig.add("s6", "vacuum.s6")
    s8 = rig.add("s8", "vacuum.s8", wet=True)
    s8.data["mop_dirty"] = False
    await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Kitchen", 3, est=2.0),
        item("s8", "vacuum.s8", "wet", "Kitchen", 3, est=10.0),
    ], wash={"s8": {"mode": "room", "interval_min": 15}})
    await rig.poll()
    assert _washes(rig, "vacuum.s8") == 0


async def test_free_robot_reorders_its_queue_so_the_mop_can_start_early(monkeypatch) -> None:
    """docs/53 §4.4: the runner re-plans the rest of a free robot's queue."""
    rig = Rig(monkeypatch)
    rig.add("s6", "vacuum.s6")
    rig.add("s8", "vacuum.s8", wet=True)
    runner = await _start(rig, [
        item("s6", "vacuum.s6", "dry", "Big", 1, est=30.0),
        item("s6", "vacuum.s6", "dry", "Small", 2, est=3.0),
        item("s8", "vacuum.s8", "wet", "Small", 2, est=20.0),
        item("s8", "vacuum.s8", "wet", "Big", 1, est=20.0),
    ])
    assert rig.hass.services.sent("vacuum.s6")[0] == [2]
    assert [it["room"] for it in runner.queues["s6"]["items"]] == ["Small", "Big"]


async def test_replanning_leaves_a_room_placed_by_hand(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    rig.add("s6", "vacuum.s6")
    rig.add("s8", "vacuum.s8", wet=True)
    big = item("s6", "vacuum.s6", "dry", "Big", 1, est=30.0)
    big["manual"] = True
    runner = await _start(rig, [
        big,
        item("s6", "vacuum.s6", "dry", "Small", 2, est=3.0),
        item("s8", "vacuum.s8", "wet", "Small", 2, est=20.0),
        item("s8", "vacuum.s8", "wet", "Big", 1, est=20.0),
    ])
    assert rig.hass.services.sent("vacuum.s6")[0] == [1]
    assert [it["room"] for it in runner.queues["s6"]["items"]] == ["Big", "Small"]


async def test_a_room_that_does_not_fit_the_battery_waits_for_a_charge(monkeypatch) -> None:
    """docs/53 §5: 40 % and a 20-min room at 1 %/min would leave 20 % (< 25):
    charge to 20 + 20 + 3 = 43... at least 25 + 20 + 1 = 46 %, then go."""
    from custom_components.anyvac import battery as bm

    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6", battery=40)
    rig.coord.battery_model = lambda duid: bm.Model(drain={"dry": 1.0})
    runner = await _start(rig, [item("s6", "vacuum.s6", "dry", "A", 1, est=20.0)])
    assert rig.hass.services.sent("vacuum.s6") == []
    assert runner.queues["s6"]["phase"] == "charging"
    assert runner.queues["s6"]["charging_to"] == 46.0
    s6.data["battery"] = 45
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s6") == []
    s6.data["battery"] = 46
    await rig.poll()
    assert rig.hass.services.sent("vacuum.s6") == [[1]]


async def test_a_robot_stopped_away_from_the_dock_is_sent_home_to_charge(monkeypatch) -> None:
    from custom_components.anyvac import battery as bm

    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6", battery=30)
    s6.data.update(in_cleaning=True, status_state="idle")
    rig.coord.battery_model = lambda duid: bm.Model(drain={"dry": 1.0})
    await _start(rig, [item("s6", "vacuum.s6", "dry", "A", 1, est=20.0)])
    await rig.poll()
    assert rig.hass.services.count("vacuum", "return_to_base", "vacuum.s6") == 1


async def test_the_job_keeps_its_vacuum_roles_for_rooms_added_later(monkeypatch) -> None:
    """Field 2026-11-27: Bedroom added during a job went dry to S8 and wet to S7.
    The job remembers the `vacuums` limit it was started with (also across a
    restart) and `edit_job add` plans with it."""
    rig = Rig(monkeypatch)
    rig.add("s6", "vacuum.s6")
    roles = {"dry": ["vacuum.s6"], "wet": ["vacuum.s8"]}
    runner = jr.JobRunner(rig.hass, [item("s6", "vacuum.s6", "dry", "A", 1)], mode="both", vacuums=roles)
    assert runner.snapshot()["vacuums"] == roles
    again = jr.JobRunner.from_snapshot(rig.hass, runner.snapshot())
    assert again is not None and again.vacuums == roles


async def test_queued_rooms_take_the_current_learned_times(monkeypatch) -> None:
    """Field 2026-11-27: the queue kept the estimates from the job's start."""
    from custom_components.anyvac import planner as pl

    class P:
        def __init__(self, hass, coord):
            pass

        def _estimate(self, duid, room, kind, repeat=1, route=None):
            return {"A": 3.0, "B": 7.0}[room] * repeat

    monkeypatch.setattr(pl, "CleanPlanner", P)
    rig = Rig(monkeypatch)
    s6 = rig.add("s6", "vacuum.s6")
    a = item("s6", "vacuum.s6", "dry", "A", 1, est=30.0)
    b = item("s6", "vacuum.s6", "dry", "B", 2, est=30.0)
    b["repeat"] = 2
    runner = await _start(rig, [a, b])
    items = {it["room"]: it for it in runner.queues["s6"]["items"]}
    assert items["A"]["est_min"] == 3.0    # refreshed before it was sent
    assert items["B"]["est_min"] == 14.0   # 7 per pass x 2
