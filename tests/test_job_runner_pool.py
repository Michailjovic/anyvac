"""Tests for _JobRunner's progressive pool-task dispatch (docs/23).

Exercises `_dispatch_pools()`'s adaptive threshold batching purely through
the same public surface real events drive: `anyvac_room_done` /
`anyvac_clean_finished` bus events (a fake bus fires them synchronously,
matching HA's `async_fire`) and `hass.services.async_call` (recorded instead
of touching a real vacuum). `homeassistant.helpers.event.async_call_later`
is monkeypatched to a fake scheduler that records `(delay, action)` instead
of using a real asyncio timer — tests fire the action themselves to
simulate a wait-recheck firing, and can assert on the recorded delay to
confirm the wiring (not just the decision math) is correct. `dt_util.utcnow`
is monkeypatched to a manually-advanced clock, same pattern as
`test_coordinator_pipeline.py`.

No coordinator/config_entries fake is needed: all tasks below are built
with no `job_rooms`, so `_JobRunner.start()`/`finish()` never touch
`_coordinators(hass)` (the loops over `self.job_rooms` are no-ops on an
empty dict).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.anyvac.services as services_mod
from custom_components.anyvac.services import _JobRunner

pytestmark = pytest.mark.asyncio

UTC = timezone.utc


class _Clock:
    """Deterministic, manually-advanced replacement for dt_util.utcnow()."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class _FakeScheduler:
    """Replaces homeassistant.helpers.event.async_call_later: records
    scheduled callbacks instead of arming a real asyncio timer, so a test
    can fire (or ignore) them deterministically."""

    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []

    def __call__(self, hass: Any, delay_seconds: float, action: Any):
        entry = {"delay": delay_seconds, "action": action, "cancelled": False}
        self.scheduled.append(entry)

        def _cancel() -> None:
            entry["cancelled"] = True

        return _cancel

    def latest_active(self) -> dict[str, Any]:
        for entry in reversed(self.scheduled):
            if not entry["cancelled"]:
                return entry
        raise AssertionError("no pending (non-cancelled) scheduled callback")


class _FakeBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}

    def async_listen(self, event_type: str, handler: Any):
        self._listeners.setdefault(event_type, []).append(handler)

        def _unsub() -> None:
            self._listeners[event_type].remove(handler)

        return _unsub

    async def fire(self, event_type: str, data: dict[str, Any]) -> None:
        event = SimpleNamespace(data=data)
        for handler in list(self._listeners.get(event_type, [])):
            await handler(event)


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], blocking: bool = True
    ) -> None:
        self.calls.append((domain, service, dict(data)))

    def send_command_calls(self) -> list[dict[str, Any]]:
        return [d for dom, svc, d in self.calls if dom == "vacuum" and svc == "send_command"]


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()
        self.services = _FakeServices()
        self.data: dict[str, Any] = {}


def _rig(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeHass, _Clock, _FakeScheduler]:
    hass = _FakeHass()
    clock = _Clock(datetime(2026, 7, 18, 12, 0, tzinfo=UTC))
    scheduler = _FakeScheduler()
    monkeypatch.setattr(services_mod.dt_util, "utcnow", lambda: clock.now)
    monkeypatch.setattr(services_mod, "async_call_later", scheduler)
    return hass, clock, scheduler


def _pool_task(duid: str, entity: str, rooms: dict[str, tuple[str, float, int]]) -> dict[str, Any]:
    """rooms: {room_name: (gate_duid, eta_min, segment_id)}"""
    return {
        "id": f"wet-{duid}",
        "vacuum": entity,
        "duid": duid,
        "pool": {
            room: {"gate": {"duid": gate_duid, "room": room}, "eta_min": eta, "segment": seg}
            for room, (gate_duid, eta, seg) in rooms.items()
        },
        "selects": [],
        "fan_speed": None,
        "repeat": 1,
        "own_gate": None,
    }


async def test_close_together_rooms_batch_into_one_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rooms whose dry passes finish close together (gap under
    BATCH_WAIT_THRESHOLD_MIN) must be sent as ONE app_segment_clean, not two
    separate dock trips — this is the "whole apartment" case the user
    remembered working well historically."""
    hass, clock, scheduler = _rig(monkeypatch)
    task = _pool_task(
        "wet1", "vacuum.wet1",
        {"A": ("dry1", 2.0, 1), "B": ("dry1", 2.5, 2)},
    )
    runner = _JobRunner(hass, [task])
    await runner.start()
    assert hass.services.send_command_calls() == []  # nothing ready yet

    await hass.bus.fire(f"{services_mod.DOMAIN}_room_done", {"duid": "dry1", "room": "A"})
    # B is estimated only 2.5 min out (< 3.0 threshold) -> waits instead of
    # dispatching A alone.
    assert hass.services.send_command_calls() == []
    wait_entry = scheduler.latest_active()
    assert wait_entry["delay"] == pytest.approx(2.5 * 60)

    clock.advance(minutes=2.5)
    await hass.bus.fire(f"{services_mod.DOMAIN}_room_done", {"duid": "dry1", "room": "B"})

    calls = hass.services.send_command_calls()
    assert len(calls) == 1
    assert calls[0]["params"][0]["segments"] == [1, 2]
    assert runner.pool_busy.get("wet1") is not None


async def test_far_apart_rooms_split_and_second_batch_waits_for_robot_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fast room + a slow one (gap over the threshold) must dispatch the
    fast room right away rather than waiting — and the second batch must
    NOT be sent while the robot is still out on the first one, only after
    its `anyvac_clean_finished` (docs/23 §2 hard constraint: a new command
    mid-clean discards the one in progress)."""
    hass, clock, scheduler = _rig(monkeypatch)
    task = _pool_task(
        "wet1", "vacuum.wet1",
        {"Fast": ("dry1", 2.0, 1), "Slow": ("dry1", 30.0, 2)},
    )
    runner = _JobRunner(hass, [task])
    await runner.start()

    await hass.bus.fire(f"{services_mod.DOMAIN}_room_done", {"duid": "dry1", "room": "Fast"})
    calls = hass.services.send_command_calls()
    assert len(calls) == 1
    assert calls[0]["params"][0]["segments"] == [1]
    assert runner.pool_busy["wet1"] == task["id"]

    # Slow room's dry pass genuinely finishes while the robot is still
    # cleaning the first batch -> must NOT trigger a second dispatch yet.
    clock.advance(minutes=30)
    await hass.bus.fire(f"{services_mod.DOMAIN}_room_done", {"duid": "dry1", "room": "Slow"})
    assert len(hass.services.send_command_calls()) == 1

    # Robot physically finishes (mop wash + charged) -> now free for batch 2.
    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "wet1"})
    calls = hass.services.send_command_calls()
    assert len(calls) == 2
    assert calls[1]["params"][0]["segments"] == [2]
    # Both rooms are now dispatched, but the robot is out cleaning batch 2 --
    # the pool task only closes out once THAT finishes too.
    assert runner.pool_tasks
    assert runner.pool_busy["wet1"] == task["id"]

    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "wet1"})
    assert not runner.pool_tasks  # both batches finished -> task closed out
    assert not runner.pending
    assert "wet1" not in runner.pool_busy


async def test_bad_estimate_forced_out_by_wait_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the "soon" room never actually arrives, BATCH_WAIT_CAP_MIN must
    force a dispatch with whatever IS ready — a wrong estimate must never
    stall a batch indefinitely."""
    hass, clock, scheduler = _rig(monkeypatch)
    task = _pool_task(
        "wet1", "vacuum.wet1",
        {"A": ("dry1", 2.0, 1), "B": ("dry1", 2.5, 2)},
    )
    runner = _JobRunner(hass, [task])
    await runner.start()

    await hass.bus.fire(f"{services_mod.DOMAIN}_room_done", {"duid": "dry1", "room": "A"})
    assert hass.services.send_command_calls() == []
    wait_entry = scheduler.latest_active()

    # B never actually completes. Advance past BATCH_WAIT_CAP_MIN (6 min)
    # since A first became ready, then fire the scheduled recheck itself
    # (proving the timer wiring, not just the decision math).
    clock.advance(minutes=6.5)
    await wait_entry["action"](clock.now)

    calls = hass.services.send_command_calls()
    assert len(calls) == 1
    assert calls[0]["params"][0]["segments"] == [1]  # dispatched WITHOUT B
    assert runner.pool_tasks  # B is still pending -> task not closed out


async def test_static_task_bypasses_pool_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task with no "pool" key (single-room robot, or a standalone "wet"
    job — see test_planner_pool_tasks.py) must run through the existing
    `_dispatch_ready`/`after`-gated path unchanged, not `_dispatch_pools()`.
    Regression guard for the __init__ split between self.tasks/pool_tasks."""
    hass, _clock, _scheduler = _rig(monkeypatch)
    task = {
        "id": "wet0",
        "vacuum": "vacuum.wet1",
        "selects": [],
        "fan_speed": None,
        "service": "vacuum.send_command",
        "service_data": {
            "entity_id": "vacuum.wet1",
            "command": "app_segment_clean",
            "params": [{"segments": [1], "repeat": 1}],
        },
        "after": [],
    }
    runner = _JobRunner(hass, [task])
    await runner.start()

    assert not runner.pool_tasks
    assert not runner.pending  # no `after` conditions -> dispatched immediately
    calls = hass.services.send_command_calls()
    assert len(calls) == 1
    assert calls[0]["params"][0]["segments"] == [1]
    assert runner.started_vacuums == {"vacuum.wet1"}
