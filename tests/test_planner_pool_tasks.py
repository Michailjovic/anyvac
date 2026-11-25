"""Tests for CleanPlanner.build_tasks's queue items (docs/52 §9.1).

The file keeps its name from the docs/23 pool tasks it replaced (a deleted
test file would linger on the user's disk). Since 1.58.0 every robot gets a
QUEUE: one item per room and pass, in the order it will run. A batch robot
(docs/52 R7) gets one item per pass with all its rooms in the app's order.

The planner is built via ``object.__new__``; ``selects_for_duid`` (the one
helper that needs a real entity registry) is monkeypatched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.anyvac.planner import CleanPlanner


class _FakeDevice:
    def __init__(self, mop_signal: dict | None = None, dock: dict | None = None) -> None:
        self.data = {"mop_signal": mop_signal, "dock_status": dock or {}}


def _planner(monkeypatch: pytest.MonkeyPatch, selects: dict | None = None, **kw) -> CleanPlanner:
    monkeypatch.setattr(
        "custom_components.anyvac.planner.selects_for_duid",
        lambda hass, duid: (selects or {}).get(duid, {}),
    )
    planner = object.__new__(CleanPlanner)
    planner.hass = None
    planner.coord = SimpleNamespace(
        rooms_estimate=kw.get("rooms_estimate", {}), app_sequence=kw.get("app_sequence", {})
    )
    planner.devices = kw["devices"]
    planner.segments = kw["segments"]
    planner.entity_of = kw["entity_of"]
    planner.duid_of_entity = {v: k for k, v in kw["entity_of"].items() if v}
    return planner


def _fleet(monkeypatch, **kw):
    return _planner(
        monkeypatch,
        devices={"s6": _FakeDevice(), "s8": _FakeDevice(mop_signal={"water_box_mode": 200})},
        segments={
            "s6": {"Corridor": 20, "Bathroom": 19, "Living": 18},
            "s8": {"Corridor": 18, "Bathroom": 17, "Living": 16},
        },
        entity_of={"s6": "vacuum.s6", "s8": "vacuum.s8"},
        **kw,
    )


def test_one_item_per_room_and_pass_in_queue_order(monkeypatch) -> None:
    est = {
        "s6": {"Corridor": {"dry": 5}, "Bathroom": {"dry": 3}, "Living": {"dry": 20}},
        "s8": {"Corridor": {"wet": 8}, "Bathroom": {"wet": 6}, "Living": {"wet": 25}},
    }
    planner = _fleet(monkeypatch, rooms_estimate=est)
    items, plan = planner.build_tasks(["Living", "Corridor", "Bathroom"], "both", vacuums={"dry": ["vacuum.s6"], "wet": ["vacuum.s8"]})
    dry = [it for it in items if it["kind"] == "dry"]
    wet = [it for it in items if it["kind"] == "wet"]
    # Dry: shortest first, so the mop can start early.
    assert [it["room"] for it in dry] == ["Bathroom", "Corridor", "Living"]
    # Wet: in the order the dry pass of each room ends.
    assert [it["room"] for it in wet] == ["Bathroom", "Corridor", "Living"]
    assert all(it["batch"] is False and len(it["segments"]) == 1 for it in items)
    # Each robot gets its OWN segment id of the room.
    assert next(it for it in dry if it["room"] == "Living")["segments"] == [18]
    assert next(it for it in wet if it["room"] == "Living")["segments"] == [16]
    assert plan["dry"] == {"vacuum.s6": ["Bathroom", "Corridor", "Living"]}
    assert plan["queues"]["vacuum.s8"][0]["room"] == "Bathroom"


def test_item_carries_its_settings_and_what_to_check(monkeypatch) -> None:
    planner = _fleet(
        monkeypatch,
        selects={"s8": {"mop_mode": "select.s8_mop_mode", "mop_intensity": "select.s8_int"}},
    )
    settings = {"wet": {"vacuum.s8": {"fan_speed": "off", "mop_mode": "deep_plus", "mop_intensity": "standard", "repeat": 2}}}
    items, _ = planner.build_tasks(["Corridor"], "wet", settings=settings)
    (it,) = items
    assert it["repeat"] == 2
    assert it["fan_speed"] == "off"
    assert it["selects"] == [
        {"entity_id": "select.s8_mop_mode", "option": "deep_plus"},
        {"entity_id": "select.s8_int", "option": "standard"},
    ]
    assert it["expect"] == {"fan_speed": "off", "mop_mode": "deep_plus", "water_off": False}


def test_dry_pass_of_a_mop_robot_turns_the_water_off(monkeypatch) -> None:
    planner = _fleet(monkeypatch, selects={"s8": {"mop_intensity": "select.s8_int"}})
    items, _ = planner.build_tasks(["Corridor"], "dry", vacuums=["vacuum.s8"])
    (it,) = items
    assert it["selects"] == [{"entity_id": "select.s8_int", "option": "off"}]
    assert it["expect"]["water_off"] is True


def test_batch_robot_gets_one_item_per_pass_in_the_app_order(monkeypatch) -> None:
    planner = _fleet(monkeypatch, app_sequence={"s6": {"Living": 1, "Corridor": 2, "Bathroom": 3}})
    items, plan = planner.build_tasks(["Corridor", "Bathroom", "Living"], "dry", vacuums=["vacuum.s6"], batch=["vacuum.s6"])
    (it,) = items
    assert it["batch"] is True
    assert it["rooms"] == ["Living", "Corridor", "Bathroom"]
    assert it["segments"] == [18, 20, 19]
    assert plan["batch"] == ["vacuum.s6"]
