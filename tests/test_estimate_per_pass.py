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


def test_values_from_before_per_pass_are_whole_cleans_until_measured() -> None:
    """Field 2026-11-27: 402 min left for ~2 h — S6 Bathroom on 2 passes took
    5 min, the stored 4 was already both passes and got doubled."""
    c = object.__new__(AnyVacCoordinator)
    c._estimates = {"s6": {"Bathroom": {"dry": 4.0}, "Living": {"dry": 30.0}}}
    c._est_legacy = {("s6", "Bathroom", "dry"), ("s6", "Living", "dry")}
    c._delay_save = lambda *a, **k: None
    c._est_store = None
    p = object.__new__(CleanPlanner)
    p.coord = SimpleNamespace(rooms_estimate=c._estimates, estimate_is_whole_clean=c.estimate_is_whole_clean)
    p.home_room_owners, p.home_room_id_by_name = {}, {}
    assert p._estimate("s6", "Bathroom", "dry", repeat=2) == 4.0       # not doubled
    c._learn_estimate("s6", "Bathroom", "dry", 2.6)                      # first per-pass sample
    assert c._estimates["s6"]["Bathroom"]["dry"] == 2.6                  # replaced, not averaged
    assert p._estimate("s6", "Bathroom", "dry", repeat=2) == 5.2
    assert p._estimate("s6", "Living", "dry", repeat=2) == 30.0          # still a whole clean
    payload = c._est_payload()
    assert payload["__format"] == 2 and payload["__legacy"] == [["s6", "Living", "dry"]]


def test_old_whole_clean_values_can_be_converted_to_per_pass() -> None:
    c = object.__new__(AnyVacCoordinator)
    c._estimates = {"s6": {"Bathroom": {"dry": 5.0}}, "s8": {"Living": {"wet": 38.0, "dry": 3.0}}}
    c._est_legacy = {("s6", "Bathroom", "dry"), ("s8", "Living", "wet")}
    c._delay_save = lambda *a, **k: None
    c._est_store = None
    c.async_update_listeners = lambda: None
    assert c.convert_whole_cleans(2) == 2
    assert c._estimates == {"s6": {"Bathroom": {"dry": 2.5}}, "s8": {"Living": {"wet": 19.0, "dry": 3.0}}}
    assert c._est_legacy == set()
    assert c.convert_whole_cleans(2) == 0  # once only
