"""docs/44 F2 — `job_progress`: the running job's progress, published for the
card's hero bar ("Cleaning · done around HH:MM · 2 of 5 rooms").

The card must not estimate anything itself (docs/14), so everything the hero
shows is decided here. What is worth pinning down:

* the finish estimate never drops below the slowest robot's own remaining work,
  even when the planner's ETA has already elapsed (a job running late must not
  show "done around <a time in the past>");
* a room counts as done only when ALL its passes are (dry + wet);
* ``anyvac_room_done`` carries no clean type, so a both-capable robot's first
  event for a room closes its dry pass and the second its wet pass;
* the runner registers/marks/clears the plan on the coordinator — and a raw
  ``run_job`` (no plan) never touches it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.anyvac.coordinator as coord_mod
import custom_components.anyvac.services as services_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator
from custom_components.anyvac.services import _JobRunner

UTC = timezone.utc
T0 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _coord(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> AnyVacCoordinator:
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", clock)
    c = object.__new__(AnyVacCoordinator)
    c._job_state = None
    c.data = {}
    c.async_update_listeners = lambda: None  # type: ignore[method-assign]
    return c


def _dev(**data: Any) -> SimpleNamespace:
    return SimpleNamespace(data=data)


PASSES = [
    {"room": "Hall", "kind": "dry", "vacuum": "vacuum.s6", "duid": "s6", "est_min": 6, "finish_min": 6},
    {"room": "Living room", "kind": "dry", "vacuum": "vacuum.s6", "duid": "s6", "est_min": 40, "finish_min": 46},
    {"room": "Hall", "kind": "wet", "vacuum": "vacuum.s8", "duid": "s8", "est_min": 10, "finish_min": 16},
]


def test_inactive_without_a_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    assert c.job_progress == {"active": False}


def test_active_pass_uses_live_coverage_and_finish_never_undershoots_remaining_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(T0)
    c = _coord(monkeypatch, clock)
    c.set_job_plan(PASSES, eta_min=46, started_at=T0)
    clock.now = T0 + timedelta(minutes=10)
    c.data = {
        "s6": _dev(in_cleaning=True, vacuum_room_name="Hall", rooms_progress={"Hall": {"dry_pct": 50}}),
        "s8": _dev(in_cleaning=False),
    }
    jp = c.job_progress
    assert jp["active"] is True
    hall_dry = next(r for r in jp["rooms"] if r["room"] == "Hall" and r["kind"] == "dry")
    assert hall_dry == {"room": "Hall", "kind": "dry", "vacuum": "vacuum.s6", "state": "active", "pct": 50}
    # S6 still owes half of Hall (3) + all of Living room (40) = 43 min, more than
    # the planner's 46 - 10 elapsed = 36 → the robot's own work wins.
    assert jp["eta_min_left"] == 43.0
    assert jp["finish_at"] == (clock.now + timedelta(minutes=43)).isoformat(timespec="seconds")
    assert jp["vacuums"]["vacuum.s6"] == {"room": "Hall", "kind": "dry", "pct": 50, "next_room": "Living room"}
    assert jp["vacuums"]["vacuum.s8"]["room"] is None
    assert jp["vacuums"]["vacuum.s8"]["next_room"] == "Hall"


def test_planner_eta_wins_while_gating_still_holds_work_back(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(T0)
    c = _coord(monkeypatch, clock)
    c.set_job_plan(PASSES, eta_min=90, started_at=T0)  # e.g. a long wet tail after the dry pass
    clock.now = T0 + timedelta(minutes=5)
    jp = c.job_progress
    assert jp["eta_min_left"] == 85.0


def test_a_room_is_done_only_when_all_its_passes_are(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_plan(PASSES, eta_min=46, started_at=T0)
    c.mark_job_room_done("s6", "Hall")
    jp = c.job_progress
    assert (jp["rooms_done"], jp["rooms_total"]) == (0, 2)
    assert (jp["passes_done"], jp["passes_total"]) == (1, 3)
    c.mark_job_room_done("s8", "Hall")
    jp = c.job_progress
    assert (jp["rooms_done"], jp["rooms_total"]) == (1, 2)


def test_both_capable_robot_closes_dry_then_wet(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_plan(
        [
            {"room": "Kitchen", "kind": "wet", "vacuum": "vacuum.s8", "duid": "s8", "est_min": 12, "finish_min": 29},
            {"room": "Kitchen", "kind": "dry", "vacuum": "vacuum.s8", "duid": "s8", "est_min": 17, "finish_min": 17},
        ],
        eta_min=29,
        started_at=T0,
    )
    c.mark_job_room_done("s8", "Kitchen")
    states = {r["kind"]: r["state"] for r in c.job_progress["rooms"]}
    assert states == {"dry": "done", "wet": "queued"}
    c.mark_job_room_done("s8", "Kitchen")
    states = {r["kind"]: r["state"] for r in c.job_progress["rooms"]}
    assert states == {"dry": "done", "wet": "done"}
    assert c.job_progress["rooms_done"] == 1


def test_clear_returns_to_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_plan(PASSES, eta_min=46, started_at=T0)
    c.clear_job_plan()
    assert c.job_progress == {"active": False}


# -- runner wiring -----------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}

    def async_listen(self, et: str, h: Any):
        self.listeners.setdefault(et, []).append(h)
        return lambda: self.listeners[et].remove(h)

    async def fire(self, et: str, data: dict[str, Any]) -> None:
        for h in list(self.listeners.get(et, [])):
            await h(SimpleNamespace(data=data))


class _Svc:
    async def async_call(self, *a: Any, **k: Any) -> None:
        return None


class _RecCoord:
    def __init__(self) -> None:
        self.log: list[tuple] = []

    def set_job_rooms(self, duid: str, rooms: Any) -> None:
        pass

    def set_job_plan(self, passes: list, eta: float, started: Any) -> None:
        self.log.append(("plan", len(passes), eta))

    def mark_job_room_done(self, duid: str, room: str) -> None:
        self.log.append(("done", duid, room))

    def clear_job_plan(self) -> None:
        self.log.append(("clear",))


class _BareCoord:
    """No job-progress methods at all — a raw run_job must never call them."""

    def set_job_rooms(self, duid: str, rooms: Any) -> None:
        pass


def _task() -> dict[str, Any]:
    return {
        "id": "dry0", "vacuum": "vacuum.s6", "duid": "s6", "selects": [], "fan_speed": None,
        "service": "vacuum.send_command",
        "service_data": {"entity_id": "vacuum.s6", "command": "app_segment_clean", "params": [{"segments": [1]}]},
    }


@pytest.mark.asyncio
async def test_runner_registers_marks_and_clears_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = SimpleNamespace(bus=_Bus(), services=_Svc(), data={})
    coord = _RecCoord()
    monkeypatch.setattr(services_mod, "_coordinators", lambda h: [coord])
    monkeypatch.setattr(services_mod, "async_call_later", lambda *a, **k: (lambda: None))
    monkeypatch.setattr(services_mod.dt_util, "utcnow", lambda: T0)
    runner = _JobRunner(hass, [_task()], {"s6": {"Hall"}}, {"passes": PASSES[:1], "eta_min": 6})
    await runner.start()
    assert coord.log == [("plan", 1, 6)]
    await hass.bus.fire("anyvac_room_done", {"duid": "s6", "room": "Hall"})
    assert coord.log[-1] == ("done", "s6", "Hall")
    await hass.bus.fire("anyvac_clean_finished", {"duid": "s6"})
    assert coord.log[-1] == ("clear",)


@pytest.mark.asyncio
async def test_raw_run_job_without_a_plan_never_touches_job_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = SimpleNamespace(bus=_Bus(), services=_Svc(), data={})
    monkeypatch.setattr(services_mod, "_coordinators", lambda h: [_BareCoord()])
    monkeypatch.setattr(services_mod, "async_call_later", lambda *a, **k: (lambda: None))
    monkeypatch.setattr(services_mod.dt_util, "utcnow", lambda: T0)
    runner = _JobRunner(hass, [_task()], {"s6": {"Hall"}})
    await runner.start()  # would raise AttributeError on _BareCoord if it tried
    await hass.bus.fire("anyvac_room_done", {"duid": "s6", "room": "Hall"})
    await hass.bus.fire("anyvac_clean_finished", {"duid": "s6"})


def test_job_progress_is_published_and_unrecorded() -> None:
    from custom_components.anyvac.sensor import AnyVacMapSensor

    assert "job_progress" in AnyVacMapSensor._unrecorded_attributes
