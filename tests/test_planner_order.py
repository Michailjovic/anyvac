"""docs/53: travel between rooms and the order search."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.anyvac import transit as tr
from custom_components.anyvac.planner import CHAIN_GAP_MIN, CleanPlanner, simulate_queues


def _planner(est: dict, transit: dict, wet: set[str] = frozenset(), segs: dict | None = None) -> CleanPlanner:
    p = object.__new__(CleanPlanner)
    p.hass = None
    p.coord = SimpleNamespace(
        rooms_estimate=est,
        transit_minutes=lambda d: transit.get(d, {}),
        robot_place=lambda d: "dock",
    )
    p.devices = {
        d: SimpleNamespace(data={"dock_status": {}, "mop_signal": {"water_box_mode": 202} if d in wet else {}})
        for d in est
    }
    p.segments = segs or {d: {r: i for i, r in enumerate(rooms)} for d, rooms in est.items()}
    p.entity_of = {d: f"vacuum.{d}" for d in est}
    p.duid_of_entity = {v: k for k, v in p.entity_of.items()}
    p.home_room_owners, p.home_room_id_by_name = {}, {}
    return p


def _line(*names: str, step: float = 2.0) -> dict[str, dict[str, float]]:
    """Rooms on a line, `dock` first: travel = distance × step minutes."""
    pos = {n: i for i, n in enumerate(names)}
    return {a: {b: abs(pos[a] - pos[b]) * step for b in names if b != a} for a in names}


def test_travel_keeps_the_robot_from_zigzagging(monkeypatch) -> None:
    """Rooms on a line from the dock: sorting by size alone (C is the smallest)
    sends the robot to the far end and back; with travel the order follows the
    line (docs/53 §2.2)."""
    monkeypatch.setattr("custom_components.anyvac.planner.selects_for_duid", lambda h, d: {})
    line = _line("dock", "A", "B", "C")
    p = _planner({"s6": {"A": {"dry": 5}, "B": {"dry": 5}, "C": {"dry": 2}}}, {"s6": line})
    _items, plan = p.build_tasks(["A", "B", "C"], "dry")
    assert plan["dry"]["vacuum.s6"] == ["A", "B", "C"]
    assert plan["eta_min"] == round(2 + 5 + 2 + 5 + 2 + 2 + 2 * CHAIN_GAP_MIN)


def test_small_dry_rooms_first_so_the_mop_starts_early(monkeypatch) -> None:
    monkeypatch.setattr("custom_components.anyvac.planner.selects_for_duid", lambda h, d: {})
    est = {"s6": {"Big": {"dry": 30}, "Small": {"dry": 3}},
           "s8": {"Big": {"wet": 20}, "Small": {"wet": 20}}}
    p = _planner(est, {}, wet={"s8"})
    _items, plan = p.build_tasks(["Big", "Small"], "both", vacuums={"dry": ["vacuum.s6"], "wet": ["vacuum.s8"]})
    assert plan["dry"]["vacuum.s6"] == ["Small", "Big"]
    assert plan["wet"]["vacuum.s8"] == ["Small", "Big"]


def test_a_pinned_room_keeps_its_robot(monkeypatch) -> None:
    monkeypatch.setattr("custom_components.anyvac.planner.selects_for_duid", lambda h, d: {})
    est = {"s6": {"A": {"dry": 20}, "B": {"dry": 20}}, "s7": {"A": {"dry": 20}, "B": {"dry": 20}}}
    p = _planner(est, {})
    _i, plan = p.build_tasks(["A", "B"], "dry", pin={"A": {"dry": "vacuum.s6"}, "B": {"dry": "vacuum.s6"}})
    assert plan["dry"] == {"vacuum.s6": ["A", "B"]}
    _i, plan = p.build_tasks(["A", "B"], "dry")
    assert sorted(len(v) for v in plan["dry"].values()) == [1, 1]  # spread when free


def test_simulation_travel_starts_from_where_the_robot_is() -> None:
    line = _line("dock", "A", "B")
    r = {"items": [{"id": "x", "kind": "dry", "rooms": ["B"], "est_min": 5}], "transit": line}
    assert simulate_queues({"r": {**r, "place": "dock"}})["x"] == (0.0, 9.0)
    assert simulate_queues({"r": {**r, "place": "A"}})["x"] == (0.0, 7.0)
    # An item already under way has no travel left.
    r["items"][0]["here"] = True
    assert simulate_queues({"r": r})["x"] == (0.0, 5.0)


def test_floor_minutes_follows_the_floor_not_the_straight_line() -> None:
    """Two rooms side by side with a wall between them: the way round is longer."""
    g = np.zeros((10, 21), dtype=np.uint8)
    g[0:10, 0:10] = 1          # room 1
    g[0:10, 11:21] = 2         # room 2
    g[9, 10] = 3               # the only door, at the bottom
    m = tr.floor_minutes(g, 0, 0, {1: "A", 2: "B", 3: "Door"}, {"x": 2 * 50, "y": 2 * 50})
    straight = 10 * 50 / tr.TRAVEL_MM_S / 60  # ~10 cells
    assert m["A"]["B"] > straight * 1.5
    assert m["A"]["B"] == pytest.approx(m["B"]["A"])
    assert "dock" in m and m["dock"]["A"] < m["dock"]["B"]


def test_learned_transit_overrides_the_floor_model() -> None:
    assert tr.blend({"A": {"B": 2.0}}, {"A": {"B": 0.7}, "dock": {"A": 1.1}}) == {"A": {"B": 0.7}, "dock": {"A": 1.1}}


def test_the_room_under_way_comes_first_and_a_ready_room_before_a_waiting_one() -> None:
    """Field 2026-11-27: S8's queue was Corridor, Kitchen, Bathroom; the runner
    sent Bathroom (the only room whose dry pass was done) and the model still
    timed Bathroom after the other two."""
    dry = {"items": [{"id": "d-cor", "kind": "dry", "rooms": ["Corridor"], "est_min": 4, "here": True},
                     {"id": "d-kit", "kind": "dry", "rooms": ["Kitchen"], "est_min": 10}]}
    wet = {"items": [{"id": "w-cor", "kind": "wet", "rooms": ["Corridor"], "est_min": 5},
                     {"id": "w-kit", "kind": "wet", "rooms": ["Kitchen"], "est_min": 5},
                     {"id": "w-bath", "kind": "wet", "rooms": ["Bathroom"], "est_min": 6, "here": True}]}
    t = simulate_queues({"s6": dry, "s8": wet})
    assert t["w-bath"] == (0.0, 6.0)
    assert t["w-cor"][0] == pytest.approx(6.35)      # Corridor dry ended at 4
    assert t["w-kit"][0] >= t["d-kit"][1]
