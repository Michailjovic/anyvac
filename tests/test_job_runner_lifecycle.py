"""Tests for _JobRunner's job LIFETIME (2026-08-08 fix).

`pending` only ever tracked tasks not yet DISPATCHED, so `_maybe_finish()`
declared a job over the moment the last command was sent — before the robot had
even started moving. Every `mode: dry` job and every `mode: wet` job hit this,
because only `mode: both` builds `after` gates or pool tasks; a job with all
ungated static tasks emptied `pending` inside `start()` itself.

Three things silently depended on the job still being registered:
  * `anyvac.cancel` (`_cancel_jobs`) — nothing to cancel means no
    `return_to_base`, i.e. the card's CANCEL bar did nothing during a dry clean;
  * plan-scope transit labeling (docs/17 §1.3) — `set_job_rooms(duid, None)`
    ran before the first poll could use the scope;
  * cross-sortie path stitching (docs/27) — `_sortie_is_new_job()` sees no
    active scope and wipes the trace on every sortie.

The tests below drive the real `_JobRunner` through the same surface real
events do (fake bus firing `anyvac_clean_finished`, recorded service calls) and
assert on the observable consequences rather than on the internal set, so they
stay meaningful if the bookkeeping is ever reshaped. Harness mirrors
`test_job_runner_pool.py`; a fake coordinator is needed here (unlike there)
precisely because these jobs DO carry `job_rooms`.
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


class _FakeBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}

    def async_listen(self, event_type: str, handler: Any):
        self._listeners.setdefault(event_type, []).append(handler)

        def _unsub() -> None:
            self._listeners[event_type].remove(handler)

        return _unsub

    async def fire(self, event_type: str, data: dict[str, Any]) -> None:
        for handler in list(self._listeners.get(event_type, [])):
            await handler(SimpleNamespace(data=data))


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


class _FakeCoord:
    """Records the plan-scope lifecycle so a test can assert when the scope is
    live — that's the observable docs/17 §1.3 behaviour, not an internal."""

    def __init__(self) -> None:
        self.job_rooms: dict[str, set[str]] = {}
        self.calls: list[tuple[str, Any]] = []

    def set_job_rooms(self, duid: str, rooms: set[str] | None) -> None:
        self.calls.append((duid, rooms))
        if rooms is None:
            self.job_rooms.pop(duid, None)
        else:
            self.job_rooms[duid] = set(rooms)


def _rig(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeHass, _FakeCoord]:
    hass = _FakeHass()
    coord = _FakeCoord()
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(services_mod.dt_util, "utcnow", lambda: now)
    monkeypatch.setattr(services_mod, "async_call_later", lambda *a, **k: (lambda: None))
    monkeypatch.setattr(services_mod, "_coordinators", lambda hass: [coord])
    return hass, coord


def _static_task(tid: str, duid: str, entity: str, after: list | None = None) -> dict[str, Any]:
    """A planner-shaped static task — what `mode: dry` (and `mode: wet`) build."""
    task: dict[str, Any] = {
        "id": tid,
        "vacuum": entity,
        "duid": duid,
        "selects": [],
        "fan_speed": None,
        "service": "vacuum.send_command",
        "service_data": {
            "entity_id": entity,
            "command": "app_segment_clean",
            "params": [{"segments": [1, 2], "repeat": 1}],
        },
    }
    if after is not None:
        task["after"] = after
    return task


async def test_ungated_dry_job_stays_alive_until_the_robot_reports_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core regression: a dry-only job must remain registered (and keep its
    plan scope) from dispatch until `anyvac_clean_finished` — not end inside
    start()."""
    hass, coord = _rig(monkeypatch)
    runner = _JobRunner(
        hass, [_static_task("dry0", "duid-s6", "vacuum.s6")], {"duid-s6": {"Kitchen", "Bedroom"}}
    )
    await runner.start()

    # Command went out...
    assert len(hass.services.send_command_calls()) == 1
    # ...and the job is still live: registered, scope applied, cancellable.
    assert runner in services_mod._active_jobs(hass)
    assert coord.job_rooms == {"duid-s6": {"Kitchen", "Bedroom"}}
    assert runner.started_vacuums == {"vacuum.s6"}

    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "duid-s6"})

    # Only now is it over, and the scope is handed back.
    assert runner not in services_mod._active_jobs(hass)
    assert coord.job_rooms == {}


async def test_wet_only_job_behaves_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mode: wet` builds a static task with an EMPTY `after` list (planner.py:
    the `if mode == "both"` branch is skipped), which is just as ungated as a
    dry task — it hit the same bug and needs the same guarantee."""
    hass, coord = _rig(monkeypatch)
    runner = _JobRunner(
        hass, [_static_task("wet0", "duid-s8", "vacuum.s8", after=[])], {"duid-s8": {"Bath"}}
    )
    await runner.start()

    assert runner in services_mod._active_jobs(hass)
    assert coord.job_rooms == {"duid-s8": {"Bath"}}

    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "duid-s8"})
    assert runner not in services_mod._active_jobs(hass)


async def test_cancel_during_an_ungated_job_sends_the_robot_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-visible half: `anyvac.cancel` -> `_cancel_jobs()` must report the
    dispatched vacuum so the handler can call `return_to_base`. Before the fix
    this returned an empty set and the CANCEL bar did nothing."""
    hass, _coord = _rig(monkeypatch)
    runner = _JobRunner(
        hass, [_static_task("dry0", "duid-s6", "vacuum.s6")], {"duid-s6": {"Kitchen"}}
    )
    await runner.start()

    started = services_mod._cancel_jobs(hass)
    assert started == {"vacuum.s6"}
    assert runner not in services_mod._active_jobs(hass)


async def test_multi_vacuum_job_waits_for_every_dispatched_robot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two dry robots: one finishing must not close the job out from under the
    other (which would clear the second robot's plan scope mid-clean)."""
    hass, coord = _rig(monkeypatch)
    runner = _JobRunner(
        hass,
        [
            _static_task("dry0", "duid-s6", "vacuum.s6"),
            _static_task("dry1", "duid-s7", "vacuum.s7"),
        ],
        {"duid-s6": {"Kitchen"}, "duid-s7": {"Bedroom"}},
    )
    await runner.start()
    assert len(hass.services.send_command_calls()) == 2

    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "duid-s6"})
    assert runner in services_mod._active_jobs(hass), "S7 is still out"
    # Scope is released per JOB, not per vacuum: S6's entry deliberately stays
    # until the whole job ends. Clearing it the moment S6's first session
    # finishes would break progressive dispatch (docs/23), where the same robot
    # comes back for another sortie inside the same job and `_sortie_is_new_job`
    # must still see an active scope with an unchanged job id.
    assert coord.job_rooms == {"duid-s6": {"Kitchen"}, "duid-s7": {"Bedroom"}}

    await hass.bus.fire(f"{services_mod.DOMAIN}_clean_finished", {"duid": "duid-s7"})
    assert runner not in services_mod._active_jobs(hass)
    assert coord.job_rooms == {}


async def test_raw_run_job_task_without_duid_keeps_dispatch_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`anyvac.run_job` accepts hand-written task lists (docs/14 §5, internal)
    that carry no `duid`. Those have nothing to match a finish event against, so
    they must keep the OLD behaviour — finish on dispatch — rather than hanging
    until JOB_TIMEOUT_SECONDS."""
    hass, _coord = _rig(monkeypatch)
    task = _static_task("raw0", "unused", "vacuum.s6")
    del task["duid"]
    runner = _JobRunner(hass, [task])
    await runner.start()

    assert len(hass.services.send_command_calls()) == 1
    assert runner not in services_mod._active_jobs(hass)


async def test_failed_dispatch_does_not_hold_the_job_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task whose service call raises never actually started a robot, so it
    must not keep the job (and its plan scope) alive waiting for a finish event
    that can never come."""
    hass, coord = _rig(monkeypatch)

    async def _boom(domain: str, service: str, data: dict[str, Any], blocking: bool = True) -> None:
        raise RuntimeError("vacuum unreachable")

    monkeypatch.setattr(hass.services, "async_call", _boom)
    runner = _JobRunner(
        hass, [_static_task("dry0", "duid-s6", "vacuum.s6")], {"duid-s6": {"Kitchen"}}
    )
    await runner.start()

    assert runner not in services_mod._active_jobs(hass)
    assert coord.job_rooms == {}


async def test_timeout_still_tears_down_a_robot_that_never_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety net: an offline robot that never fires `clean_finished` must not
    pin the job (and the plan scope) open forever — JOB_TIMEOUT_SECONDS still
    wins. Captures the scheduled timeout callback and fires it directly."""
    hass, coord = _rig(monkeypatch)
    captured: list[Any] = []
    monkeypatch.setattr(
        services_mod,
        "async_call_later",
        lambda _hass, _delay, action: (captured.append(action) or (lambda: None)),
    )
    runner = _JobRunner(
        hass, [_static_task("dry0", "duid-s6", "vacuum.s6")], {"duid-s6": {"Kitchen"}}
    )
    await runner.start()
    assert runner in services_mod._active_jobs(hass)

    assert captured, "the job timeout should have been scheduled"
    captured[0](None)  # _on_timeout

    assert runner not in services_mod._active_jobs(hass)
    assert coord.job_rooms == {}
