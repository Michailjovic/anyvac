"""docs/52 §5a: estimates are per pass and per mop route (field: S8 Living
room on 2 passes was estimated at about half its real time)."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.anyvac import coverage as covmod
from custom_components.anyvac.coordinator import AnyVacCoordinator
from custom_components.anyvac.planner import CleanPlanner


def _planner(estimates: dict) -> CleanPlanner:
    p = object.__new__(CleanPlanner)
    p.coord = SimpleNamespace(rooms_estimate={"s8": estimates})
    p.home_room_owners = {}
    p.home_room_id_by_name = {}
    return p


def test_estimate_multiplies_by_passes_and_prefers_the_mop_route() -> None:
    p = _planner({"Living": {"wet": 20.0, "wet@deep_plus": 31.0, "dry": 12.0}})
    assert p._estimate("s8", "Living", "wet") == 20.0
    assert p._estimate("s8", "Living", "wet", repeat=2) == 40.0
    assert p._estimate("s8", "Living", "wet", repeat=2, route="deep_plus") == 62.0
    assert p._estimate("s8", "Living", "wet", repeat=1, route="standard") == 20.0  # no route sample yet
    assert p._estimate("s8", "Living", "dry", repeat=2, route="deep_plus") == 24.0  # route is a wet thing


def test_item_estimate_reads_repeat_and_route_from_the_vacuums_settings() -> None:
    p = _planner({"Living": {"wet": 20.0, "wet@deep_plus": 31.0}})
    p.duid_of_entity = {"vacuum.s8": "s8"}
    p.devices = {"s8": object()}
    settings = {"wet": {"vacuum.s8": {"repeat": 2, "mop_mode": "deep_plus"}}}
    assert p._item_estimate("s8", "Living", "wet", settings) == 62.0
    assert p._item_estimate("s8", "Kitchen", "wet", settings) == 30.0  # default 15 per pass


def test_learning_is_per_pass_and_per_route() -> None:
    c = object.__new__(AnyVacCoordinator)
    c._estimates = {}
    c._delay_save = lambda *a, **k: None
    c._est_store = None
    run = covmod.RunTracker()
    run.passes = 2
    run.route = "deep_plus"
    # What `_harvest_run` does for a 2-pass wet room of 50 active minutes.
    per_pass = 50 * 60 / 60 / run.passes
    c._learn_estimate("s8", "Living", "wet", per_pass)
    c._learn_estimate("s8", "Living", f"wet@{run.route}", per_pass)
    assert c._estimates["s8"]["Living"] == {"wet": 25, "wet@deep_plus": 25}
    assert c._learn_estimate("s8", "Living", "wet@deep_plus", 0.5) == (None, None)  # under 1 min
