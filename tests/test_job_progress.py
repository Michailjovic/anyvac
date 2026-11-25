"""`job_progress` v2 (docs/52 §9.4, docs/44 F2): the card-facing snapshot of
the running job, built by the coordinator from the runner's queues with the
same timing model the plan uses (`planner.simulate_queues`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.anyvac.coordinator as coord_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator
from custom_components.anyvac.planner import CHAIN_GAP_MIN

UTC = timezone.utc
T0 = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _coord(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> AnyVacCoordinator:
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", clock)
    c = object.__new__(AnyVacCoordinator)
    c._pending_saves = {}
    c._job_state = None
    c.data = {}
    c.async_update_listeners = lambda: None  # type: ignore[method-assign]
    return c


def _it(id_: str, room: str, kind: str, state: str, est: float, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "room": room, "rooms": [room], "kind": kind, "state": state, "est_min": est, **kw}


def _state(queues: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": "j1", "started_at": T0, "mode": "both", "queues": queues, "warnings": []}


def _queues(s6_items, s8_items) -> dict[str, Any]:
    return {
        "s6": {"vacuum": "vacuum.s6", "batch": False, "items": s6_items, "phase": "cleaning", "wash": {}},
        "s8": {"vacuum": "vacuum.s8", "batch": False, "items": s8_items, "phase": "waiting_gate",
               "wash": {"mode": None}},
    }


def test_inactive_without_a_job(monkeypatch) -> None:
    assert _coord(monkeypatch, _Clock(T0)).job_progress == {"active": False}


def test_active_item_uses_live_coverage_and_gates_the_wet_robot(monkeypatch) -> None:
    clock = _Clock(T0 + timedelta(minutes=10))
    c = _coord(monkeypatch, clock)
    c.data = {"s6": SimpleNamespace(data={"rooms_progress": {"Living": {"dry_pct": 75}}})}
    c.set_job_state(_state(_queues(
        [_it("a", "Hall", "dry", "done", 6), _it("b", "Living", "dry", "active", 40)],
        [_it("c", "Living", "wet", "queued", 20)],
    )))
    jp = c.job_progress
    assert jp["active"] and jp["schema"] == 2 and jp["job_id"] == "j1"
    s6 = jp["queues"]["vacuum.s6"]["items"]
    assert [r["state"] for r in s6] == ["done", "active"]
    assert s6[1]["pct"] == 75
    # 25 % of 40 min left; the wet pass of Living waits for it, then 20 min.
    assert jp["eta_min_left"] == pytest.approx(10 + 20)
    wet = jp["queues"]["vacuum.s8"]["items"][0]
    assert wet["start_at"] == (clock.now + timedelta(minutes=10)).isoformat(timespec="seconds")
    # v1 fields stay for older cards.
    assert jp["rooms_total"] == 2 and jp["rooms_done"] == 1
    assert {r["state"] for r in jp["rooms"]} == {"done", "active", "queued"}
    assert jp["vacuums"]["vacuum.s6"]["room"] == "Living"
    assert jp["vacuums"]["vacuum.s8"]["next_room"] == "Living"


def test_removed_items_disappear_and_blocked_ones_keep_their_reason(monkeypatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_state(_state(_queues(
        [_it("a", "Hall", "dry", "removed", 6), _it("b", "Bath", "dry", "queued", 4)],
        [_it("c", "Kitchen", "wet", "blocked", 10, reason="water_shortage")],
    )))
    jp = c.job_progress
    assert [r["id"] for r in jp["queues"]["vacuum.s6"]["items"]] == ["b"]
    blocked = jp["queues"]["vacuum.s8"]["items"][0]
    assert blocked["state"] == "blocked" and blocked["reason"] == "water_shortage"
    assert jp["eta_min_left"] == pytest.approx(4)


def test_chained_rooms_include_the_gap(monkeypatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_state(_state({"s6": {"vacuum": "vacuum.s6", "items": [
        _it("a", "A", "dry", "queued", 5), _it("b", "B", "dry", "queued", 5)]}}))
    assert c.job_progress["eta_min_left"] == pytest.approx(10 + CHAIN_GAP_MIN, abs=0.1)


def test_clear_returns_to_inactive(monkeypatch) -> None:
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_state(_state({}))
    c.set_job_state(None)
    assert c.job_progress == {"active": False}


def test_job_progress_is_published_and_unrecorded() -> None:
    from custom_components.anyvac.sensor import AnyVacMapSensor

    assert "job_progress" in AnyVacMapSensor._unrecorded_attributes


def test_a_waiting_wet_item_names_the_robots_it_waits_for(monkeypatch) -> None:
    """The card's lock ("after S6", docs/52 §5 item 3): a queued wet item lists
    the robots whose dry pass of the same room is not over yet."""
    c = _coord(monkeypatch, _Clock(T0))
    c.set_job_state(_state(_queues(
        [_it("a", "Hall", "dry", "done", 6), _it("b", "Living", "dry", "active", 40),
         _it("x", "Pantry", "dry", "blocked", 3, reason="error")],
        [_it("c", "Hall", "wet", "queued", 8), _it("d", "Living", "wet", "queued", 20),
         _it("e", "Pantry", "wet", "queued", 3), _it("f", "Living", "wet", "done", 2)],
    )))
    wet = {r["id"]: r for r in c.job_progress["queues"]["vacuum.s8"]["items"]}
    assert wet["c"]["waits_for"] == []            # Hall's dry pass is done
    assert wet["d"]["waits_for"] == ["vacuum.s6"]  # Living is still being vacuumed
    assert wet["e"]["waits_for"] == []            # a blocked dry pass opens the gate
    assert "waits_for" not in wet["f"]            # only queued wet items carry it
    dry = c.job_progress["queues"]["vacuum.s6"]["items"]
    assert all("waits_for" not in r for r in dry)
