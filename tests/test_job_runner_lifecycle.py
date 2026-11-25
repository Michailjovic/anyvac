"""Job lifetime, edits and restart of the room-by-room runner (docs/52 §3.4,
§9.5) — against the fake fleet in `_fake_robot`."""

from __future__ import annotations

import pytest

import custom_components.anyvac.jobrunner as jr
import custom_components.anyvac.services as services_mod
from tests._fake_robot import Rig, item

pytestmark = pytest.mark.asyncio


async def _start(rig: Rig, items, **kw) -> jr.JobRunner:
    runner = jr.JobRunner(rig.hass, items, **kw)
    await runner.start()
    await rig.drain()
    return runner


def _three(rig):
    s6 = rig.add("s6", "vacuum.s6")
    return s6, [
        item("s6", "vacuum.s6", "dry", "A", 1),
        item("s6", "vacuum.s6", "dry", "B", 2),
        item("s6", "vacuum.s6", "dry", "C", 3),
    ]


async def test_job_stays_registered_until_the_robot_reports(monkeypatch) -> None:
    """A dry job used to end the moment its command was sent (2026-08-08 B2):
    CANCEL then had nothing to cancel."""
    rig = Rig(monkeypatch)
    _s6, items = _three(rig)
    runner = await _start(rig, items[:1])
    assert runner in jr._active_jobs(rig.hass)
    assert rig.coord.job_rooms == {"s6": {"A"}}
    assert services_mod._cancel_jobs(rig.hass) == {"vacuum.s6"}
    assert runner not in jr._active_jobs(rig.hass)
    assert rig.coord.job_rooms == {}


async def test_removing_a_queued_room_never_sends_it(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items)
    runner.remove_room("B")
    await rig.drain()
    await rig.poll()
    await rig.finish_room(s6, ["A"])
    await rig.poll()
    await rig.finish_room(s6, ["C"])
    assert rig.hass.services.sent() == [[1], [3]]
    assert rig.coord.job_rooms == {}  # finished: B removed, A and C done


async def test_removing_the_room_being_cleaned_pauses_and_goes_on(monkeypatch) -> None:
    """S6: the robot leaves the room right away, nothing of it is learned."""
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items)
    await rig.poll()
    runner.remove_room("A")
    await rig.drain()
    assert rig.hass.services.count("vacuum", "pause", "vacuum.s6") == 1
    assert rig.coord.discarded == ["s6"]
    await rig.poll()  # paused -> the next room
    assert rig.hass.services.sent() == [[1], [2]]
    assert s6.taken == [[1], [2]]


async def test_removing_the_last_room_sends_the_robot_home(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items[:1])
    await rig.poll()
    runner.remove_room("A")
    await rig.drain()
    await rig.poll()
    assert rig.hass.services.count("vacuum", "return_to_base", "vacuum.s6") == 1
    assert runner not in jr._active_jobs(rig.hass)


async def test_added_room_joins_the_queue_and_moves_are_manual(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items[:2])
    runner.add_items([item("s6", "vacuum.s6", "dry", "C", 3)])
    assert runner.move("C", "dry", 0)
    await rig.drain()
    await rig.poll()
    await rig.finish_room(s6, ["A"])
    assert rig.hass.services.sent() == [[1], [3]]
    moved = next(it for it in runner.items() if it["room"] == "C")
    assert moved["manual"] is True
    # Adding a room the job already has does nothing.
    assert runner.add_items([item("s6", "vacuum.s6", "dry", "B", 2)]) == []


async def test_batch_in_progress_cannot_be_changed(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    rig.add("s6", "vacuum.s6")
    batch = item("s6", "vacuum.s6", "dry", "A", 1, batch=True)
    batch.update(rooms=["A", "B"], segments=[1, 2], room=None)
    runner = await _start(rig, [batch])
    await rig.poll()
    with pytest.raises(ValueError):
        runner.remove_room("A")


async def test_reassigning_a_queued_room_moves_it_to_the_other_robot(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    rig.add("s7", "vacuum.s7")
    runner = await _start(rig, items[:2])
    runner.reassign("B", "dry", item("s7", "vacuum.s7", "dry", "B", 22))
    await rig.drain()
    assert rig.hass.services.sent("vacuum.s7") == [[22]]
    assert next(it for it in runner.queues["s6"]["items"] if it["room"] == "B")["state"] == "removed"


async def test_job_is_saved_and_resumes_after_a_restart(monkeypatch) -> None:
    """R3: a restart while S6 cleans A — the robot still cleans A, so the job
    simply carries on; B follows."""
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items[:2])
    await rig.poll()
    snap = rig.coord.saved[-1]
    assert snap["queues"]["s6"]["items"][0]["state"] == "active"
    runner.finish(keep_saved=True)
    assert rig.coord.saved[-1] is not None

    resumed = jr.JobRunner.from_snapshot(rig.hass, snap)
    await resumed.start(restored=True)
    await rig.drain()
    assert resumed.job_id == runner.job_id
    assert resumed.queues["s6"]["items"][0]["state"] == "active"
    await rig.finish_room(s6, ["A"])
    assert rig.hass.services.sent() == [[1], [2]]


async def test_restart_while_docked_sends_the_unfinished_room_again(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items[:2])
    await rig.poll()
    snap = rig.coord.saved[-1]
    runner.finish(keep_saved=True)
    s6.data.update(in_cleaning=False, status_state="charging", target_segments=[])
    resumed = jr.JobRunner.from_snapshot(rig.hass, snap)
    await resumed.start(restored=True)
    await rig.drain()
    # Again rather than skipped (docs/52 §9.5).
    assert rig.hass.services.sent() == [[1], [1]]


async def test_restart_while_the_robot_runs_something_else_stops_the_job(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    s6, items = _three(rig)
    runner = await _start(rig, items[:2])
    await rig.poll()
    snap = rig.coord.saved[-1]
    runner.finish(keep_saved=True)
    s6.data.update(target_segments=[9])
    resumed = jr.JobRunner.from_snapshot(rig.hass, snap)
    await resumed.start(restored=True)
    await rig.drain()
    assert all(it["state"] == "blocked" for it in resumed.items())
    assert any("cleans something else" in w for w in rig.coord.warnings)


async def test_old_saved_job_is_not_resumed(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    _s6, items = _three(rig)
    runner = await _start(rig, items[:1])
    snap = rig.coord.saved[-1]
    runner.finish()
    rig.clock.advance(hours=7)
    assert jr.JobRunner.from_snapshot(rig.hass, snap) is None
