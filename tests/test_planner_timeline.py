"""Tests for the job timing model (docs/52 §3, §5a): `simulate_queues` and the
planner's `_estimate_timeline` wrapper over it.

A room-by-room robot gets one room per command. Between two rooms it either
takes the next one on its way home (``CHAIN_GAP_MIN``) or, as a wet robot, docks
and washes first (``END_WASH_MIN``) — after every room when the app washes by
room, once the mopping since the last wash reaches the interval when it washes
by time. Inside one task the firmware washes on its own interval. A wet room
waits for the dry pass of the same room. The app's room order no longer plays
any part (docs/52 O1).

The planner is built via ``object.__new__`` (no hass / registries needed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.anyvac.planner import (
    CHAIN_GAP_MIN,
    DEFAULT_ROOM_MIN,
    END_WASH_MIN,
    PRE_WASH_MIN,
    WASH_DURATION_MIN,
    CleanPlanner,
    simulate_queues,
)

G = CHAIN_GAP_MIN


def _dev(interval: float | None = None, by_room: bool = False, washable: bool | None = None) -> SimpleNamespace:
    dock: dict = {"wash_interval_min": interval, "smart_wash_enabled": by_room}
    if washable is not None:
        dock["features"] = {"is_washable": washable}
    return SimpleNamespace(data={"dock_status": dock})


def _planner(rooms_estimate: dict, devices: dict | None = None) -> CleanPlanner:
    planner = object.__new__(CleanPlanner)
    planner.coord = SimpleNamespace(rooms_estimate=rooms_estimate)
    planner.devices = devices or {}
    planner.entity_of = {}
    return planner


def test_single_both_capable_robot_dry_then_wet() -> None:
    est = {"d1": {"Hall": {"dry": 10, "wet": 6}, "Bathroom": {"dry": 8, "wet": 5}}}
    result = _planner(est)._estimate_timeline(
        dry_assign={"d1": ["Hall", "Bathroom"]},
        wet_assign={"d1": ["Hall", "Bathroom"]},
    )
    assert result["timeline"]["dry"] == pytest.approx({"Hall": 10, "Bathroom": 18 + G})
    # No washing dock: the wet rooms are chained too.
    assert result["timeline"]["wet"] == pytest.approx({"Hall": 24 + 2 * G, "Bathroom": 29 + 3 * G})
    assert result["eta_min"] == round(29 + 3 * G)
    assert result["unsequenced"] == []


def test_wet_room_waits_for_the_dry_pass_of_the_same_room() -> None:
    """Two dry robots, one wet robot. The wet robot starts with the room whose
    dry pass ends first and waits whenever it catches up."""
    est = {
        "s6": {"Corridor": {"dry": 5}, "Living": {"dry": 30}},
        "s7": {"Bath": {"dry": 8}},
        "s8": {"Corridor": {"wet": 10}, "Bath": {"wet": 6}, "Living": {"wet": 20}},
    }
    result = _planner(est)._estimate_timeline(
        dry_assign={"s6": ["Corridor", "Living"], "s7": ["Bath"]},
        wet_assign={"s8": ["Corridor", "Bath", "Living"]},
    )
    dry = result["timeline"]["dry"]
    wet = result["timeline"]["wet"]
    assert dry["Corridor"] == 5
    assert dry["Living"] == pytest.approx(35 + G)
    assert wet["Corridor"] == pytest.approx(15)  # starts at 5 (gate)
    assert wet["Bath"] == pytest.approx(15 + G + 6)
    assert wet["Living"] == pytest.approx(35 + G + 20)  # waited for the dry pass


def test_no_rooms_assigned_gives_zero_eta() -> None:
    result = _planner({})._estimate_timeline(dry_assign={}, wet_assign={})
    assert result["eta_min"] == 0
    assert result["timeline"] == {"dry": {}, "wet": {}}


def test_default_room_min_used_when_no_estimate_exists() -> None:
    result = _planner({})._estimate_timeline(dry_assign={"d1": ["Hall"]}, wet_assign={})
    assert result["timeline"]["dry"]["Hall"] == DEFAULT_ROOM_MIN


def test_by_room_washing_docks_after_every_wet_room() -> None:
    """The app washes after every room: the robot first washes at the start
    (used mop), then docks and washes between rooms."""
    est = {"s8": {"Hall": {"wet": 10}, "Bath": {"wet": 6}}}
    result = _planner(est, {"s8": _dev(interval=20, by_room=True)})._estimate_timeline(
        dry_assign={}, wet_assign={"s8": ["Hall", "Bath"]}
    )
    assert result["timeline"]["wet"] == pytest.approx(
        {"Hall": PRE_WASH_MIN + 10, "Bath": PRE_WASH_MIN + 10 + END_WASH_MIN + 6}
    )


def test_by_time_washing_chains_until_the_interval_is_reached() -> None:
    """Washing by time (15 min): two 10-min rooms are chained (10 < 15), the
    third waits for a wash (20 >= 15)."""
    est = {"s8": {"A": {"wet": 10}, "B": {"wet": 10}, "C": {"wet": 4}}}
    result = _planner(est, {"s8": _dev(interval=15)})._estimate_timeline(
        dry_assign={}, wet_assign={"s8": ["A", "B", "C"]}
    )
    wet = result["timeline"]["wet"]
    assert wet["A"] == pytest.approx(PRE_WASH_MIN + 10)
    assert wet["B"] == pytest.approx(PRE_WASH_MIN + 20 + G)
    assert wet["C"] == pytest.approx(PRE_WASH_MIN + 20 + G + END_WASH_MIN + 4)


def test_long_room_washes_inside_the_task() -> None:
    """A 45-min room against a 15-min interval: the firmware washes three times
    inside the task (at 15, 30 and 45 min)."""
    est = {"s8": {"Kitchen": {"wet": 45}}}
    result = _planner(est, {"s8": _dev(interval=15)})._estimate_timeline(
        dry_assign={}, wet_assign={"s8": ["Kitchen"]}
    )
    assert result["timeline"]["wet"]["Kitchen"] == pytest.approx(PRE_WASH_MIN + 45 + 3 * WASH_DURATION_MIN)


def test_unreadable_washing_dock_is_treated_as_by_room() -> None:
    est = {"s8": {"A": {"wet": 5}, "B": {"wet": 5}}}
    planner = _planner(est, {"s8": _dev(washable=True)})
    assert planner.wash_mode("s8") == ("unknown", None)
    wet = planner._estimate_timeline(dry_assign={}, wet_assign={"s8": ["A", "B"]})["timeline"]["wet"]
    assert wet["B"] == pytest.approx(PRE_WASH_MIN + 5 + END_WASH_MIN + 5)


def test_no_washing_dock_means_no_washing() -> None:
    est = {"s7": {"A": {"wet": 10}, "B": {"wet": 10}}}
    planner = _planner(est, {"s7": _dev()})
    assert planner.wash_mode("s7") == (None, None)
    wet = planner._estimate_timeline(dry_assign={}, wet_assign={"s7": ["A", "B"]})["timeline"]["wet"]
    assert wet == pytest.approx({"A": 10, "B": 20 + G})


def test_batch_robot_has_no_gaps_and_counts_the_interval_across_rooms() -> None:
    """A batch is one task: no chaining gap, and the firmware's interval runs
    across its rooms."""
    est = {"s8": {"A": {"wet": 10}, "B": {"wet": 10}}}
    result = _planner(est, {"s8": _dev(interval=15)})._estimate_timeline(
        dry_assign={}, wet_assign={"s8": ["A", "B"]}, batch={"s8"}
    )
    assert result["eta_min"] == round(PRE_WASH_MIN + 20 + WASH_DURATION_MIN)


def test_simulate_live_queue_with_a_partly_done_room() -> None:
    """The live job_progress model: what is left of the active room, then the
    rest, and a wet room on another robot gated by it."""
    times = simulate_queues({
        "s6": {"items": [
            {"id": "a", "kind": "dry", "rooms": ["Living"], "est_min": 12},  # what is left
            {"id": "b", "kind": "dry", "rooms": ["Hall"], "est_min": 5},
        ]},
        "s8": {"wash_mode": None, "items": [
            {"id": "c", "kind": "wet", "rooms": ["Living"], "est_min": 20},
        ], "prev_kind": "wet"},
    })
    assert times["a"] == (0.0, 12.0)
    assert times["b"] == pytest.approx((12 + G, 17 + G))
    assert times["c"] == pytest.approx((12.0, 32.0))
