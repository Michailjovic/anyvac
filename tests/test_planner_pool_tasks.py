"""Tests for CleanPlanner.build_tasks's pool-task building (docs/23).

A wet-capable robot with 2+ assigned rooms in a "both" job gets a **pool
task** (progressive dispatch — the executor can send a first batch as soon
as SOME rooms are ready, not all of them) instead of one static
all-or-nothing task. A robot with just one room, or a standalone "wet" job
with no dry pass to gate against at all, has nothing to stagger and keeps
the plain static task — these tests lock down both branches.

Like ``test_planner_timeline.py``, the planner is built via
``object.__new__`` to skip ``CleanPlanner.__init__`` (real ``hass`` + device
registry irrelevant here) — ``selects_for_duid`` (the one helper that WOULD
need a real entity registry) is monkeypatched to a no-op.
"""

from __future__ import annotations

import pytest

from custom_components.anyvac.planner import CleanPlanner


class _FakeCoordinator:
    def __init__(self, room_sequence: dict, rooms_estimate: dict) -> None:
        self.room_sequence = room_sequence
        self.rooms_estimate = rooms_estimate


class _FakeDeviceData(dict):
    """`.data.get(...)` access, matching the real AnyVacDevice shape."""


class _FakeDevice:
    def __init__(self, mop_signal: dict | None = None) -> None:
        self.data = _FakeDeviceData(mop_signal=mop_signal)


def _planner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: dict[str, _FakeDevice],
    segments: dict[str, dict[str, int]],
    entity_of: dict[str, str],
    room_sequence: dict[str, int],
    rooms_estimate: dict[str, dict],
) -> CleanPlanner:
    monkeypatch.setattr(
        "custom_components.anyvac.planner.selects_for_duid",
        lambda hass, duid: {},
    )
    planner = object.__new__(CleanPlanner)
    planner.hass = None
    planner.coord = _FakeCoordinator(room_sequence, rooms_estimate)
    planner.devices = devices
    planner.segments = segments
    planner.entity_of = entity_of
    planner.duid_of_entity = {v: k for k, v in entity_of.items() if v}
    return planner


def test_wet_pool_task_for_multi_room_wet_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors the live-found scenario: two quick rooms (dry via a separate
    robot, done fast) + one slow room, one wet-capable robot for all three.
    It must get ONE pool task, not a static all-or-nothing task — and the
    slow room's eta_min hint must be visibly later than the quick rooms'."""
    devices = {
        "dry1": _FakeDevice(),
        "wet1": _FakeDevice(mop_signal={"water_box_mode": 200}),
    }
    segments = {
        "dry1": {"Corridor": 1, "Bathroom": 2, "LivingRoom": 3},
        "wet1": {"Corridor": 1, "Bathroom": 2, "LivingRoom": 3},
    }
    entity_of = {"dry1": "vacuum.dry1", "wet1": "vacuum.wet1"}
    room_sequence = {"Corridor": 1, "Bathroom": 2, "LivingRoom": 3}
    rooms_estimate = {
        "dry1": {"Corridor": {"dry": 5}, "Bathroom": {"dry": 5}, "LivingRoom": {"dry": 30}},
        "wet1": {"Corridor": {"wet": 6}, "Bathroom": {"wet": 6}, "LivingRoom": {"wet": 20}},
    }
    planner = _planner(
        monkeypatch, devices=devices, segments=segments, entity_of=entity_of,
        room_sequence=room_sequence, rooms_estimate=rooms_estimate,
    )

    tasks, plan = planner.build_tasks(
        ["Corridor", "Bathroom", "LivingRoom"], "both",
        vacuums={"dry": ["vacuum.dry1"], "wet": ["vacuum.wet1"]},
    )

    wet_tasks = [t for t in tasks if t["vacuum"] == "vacuum.wet1"]
    assert len(wet_tasks) == 1
    pool_task = wet_tasks[0]
    assert "pool" in pool_task
    assert "after" not in pool_task
    assert set(pool_task["pool"]) == {"Corridor", "Bathroom", "LivingRoom"}
    assert pool_task["pool"]["Corridor"]["gate"] == {"duid": "dry1", "room": "Corridor"}
    assert pool_task["pool"]["Corridor"]["segment"] == 1
    assert pool_task["pool"]["LivingRoom"]["segment"] == 3
    # LivingRoom's dry pass is estimated to finish much later than the other two.
    assert pool_task["pool"]["LivingRoom"]["eta_min"] > pool_task["pool"]["Corridor"]["eta_min"]
    assert pool_task["pool"]["LivingRoom"]["eta_min"] > pool_task["pool"]["Bathroom"]["eta_min"]
    assert pool_task.get("own_gate") is None  # wet1 runs no dry pass of its own here
    assert set(plan["wet"]["vacuum.wet1"]) == {"Corridor", "Bathroom", "LivingRoom"}


def test_wet_static_task_for_single_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """One room = nothing to stagger — stays on the plain static task."""
    devices = {
        "dry1": _FakeDevice(),
        "wet1": _FakeDevice(mop_signal={"water_box_mode": 200}),
    }
    segments = {"dry1": {"Corridor": 1}, "wet1": {"Corridor": 1}}
    entity_of = {"dry1": "vacuum.dry1", "wet1": "vacuum.wet1"}
    planner = _planner(
        monkeypatch, devices=devices, segments=segments, entity_of=entity_of,
        room_sequence={"Corridor": 1}, rooms_estimate={},
    )

    tasks, _plan = planner.build_tasks(
        ["Corridor"], "both", vacuums={"dry": ["vacuum.dry1"], "wet": ["vacuum.wet1"]},
    )

    wet_task = next(t for t in tasks if t["vacuum"] == "vacuum.wet1")
    assert "pool" not in wet_task
    assert wet_task["after"] == [{"duid": "dry1", "room": "Corridor"}]


def test_wet_alone_mode_no_pooling_even_multi_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """mode == "wet" alone has no dry pass to gate against at all — no
    benefit to pooling even with 2+ rooms, stays static with an empty
    `after` (nothing to wait for)."""
    devices = {"wet1": _FakeDevice(mop_signal={"water_box_mode": 200})}
    segments = {"wet1": {"Corridor": 1, "Bathroom": 2}}
    entity_of = {"wet1": "vacuum.wet1"}
    planner = _planner(
        monkeypatch, devices=devices, segments=segments, entity_of=entity_of,
        room_sequence={"Corridor": 1, "Bathroom": 2}, rooms_estimate={},
    )

    tasks, _plan = planner.build_tasks(["Corridor", "Bathroom"], "wet")

    wet_task = next(t for t in tasks if t["vacuum"] == "vacuum.wet1")
    assert "pool" not in wet_task
    assert wet_task["after"] == []


def test_pool_task_own_gate_for_both_capable_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A both-capable robot doing its own dry AND multi-room wet pass must
    additionally wait for its OWN dry session to finish (own_gate) before
    its first wet batch — same rule the static path already applied."""
    devices = {"both1": _FakeDevice(mop_signal={"water_box_mode": 200})}
    segments = {"both1": {"Corridor": 1, "Bathroom": 2}}
    entity_of = {"both1": "vacuum.both1"}
    planner = _planner(
        monkeypatch, devices=devices, segments=segments, entity_of=entity_of,
        room_sequence={"Corridor": 1, "Bathroom": 2}, rooms_estimate={},
    )

    tasks, _plan = planner.build_tasks(["Corridor", "Bathroom"], "both")

    pool_task = next(t for t in tasks if t["vacuum"] == "vacuum.both1" and "pool" in t)
    assert pool_task["own_gate"] == {"duid": "both1"}
