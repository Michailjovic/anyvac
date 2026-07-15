"""Tests for CleanPlanner._estimate_timeline (docs/19 sequence-aware ETA).

The Roborock app's configured room order is dominant regardless of what order
HA sends segment ids in — the firmware always visits rooms in that order
(field-confirmed across thousands of cleans). These tests lock down the
timeline math against that assumption: per-robot cumulative dry time IN
SEQUENCE ORDER, and per-room wet gating on the specific dry-assigning robot's
cumulative time for that room (not "wait for the whole dry batch").

``_estimate_timeline`` only touches ``self.coord.room_sequence`` and (via
``_estimate``) ``self.coord.rooms_estimate`` — so the planner is built via
``object.__new__`` to skip ``CleanPlanner.__init__`` (which needs a real
``hass`` + device registry, irrelevant to this pure computation).
"""

from __future__ import annotations

from custom_components.anyvac.planner import DEFAULT_ROOM_MIN, CleanPlanner


class _FakeCoordinator:
    def __init__(self, room_sequence: dict, rooms_estimate: dict) -> None:
        self.room_sequence = room_sequence
        self.rooms_estimate = rooms_estimate


def _planner(room_sequence: dict, rooms_estimate: dict) -> CleanPlanner:
    planner = object.__new__(CleanPlanner)
    planner.coord = _FakeCoordinator(room_sequence, rooms_estimate)
    return planner


def test_single_both_capable_robot_dry_then_wet() -> None:
    """One robot does its own dry pass, then its own wet pass — wet must wait
    for the robot's own dry session to finish, not start alongside it."""
    seq = {"Hall": 1, "Bathroom": 2}
    est = {"d1": {"Hall": {"dry": 10, "wet": 6}, "Bathroom": {"dry": 8, "wet": 5}}}
    planner = _planner(seq, est)

    result = planner._estimate_timeline(
        dry_assign={"d1": ["Hall", "Bathroom"]},
        wet_assign={"d1": ["Hall", "Bathroom"]},
    )

    assert result["timeline"]["dry"] == {"Hall": 10, "Bathroom": 18}
    assert result["timeline"]["wet"] == {"Hall": 24, "Bathroom": 29}
    assert result["eta_min"] == 29
    assert result["unsequenced"] == []


def test_two_dry_robots_one_wet_robot() -> None:
    """Mirrors the real field scenario: two dry robots split the shared
    sequence, one wet robot follows behind gated per-room. Bathroom's wet pass
    isn't gated (dry finishes before the wet robot even gets there from Hall),
    but LivingRoom's is (the wet robot catches up to its own dry robot)."""
    seq = {"Hall": 1, "Bathroom": 2, "LivingRoom": 3, "Kitchen": 4}
    est = {
        "d1": {"Hall": {"dry": 10}, "LivingRoom": {"dry": 12}},
        "d2": {"Bathroom": {"dry": 8}, "Kitchen": {"dry": 9}},
        "d3": {
            "Hall": {"wet": 6}, "Bathroom": {"wet": 5},
            "LivingRoom": {"wet": 7}, "Kitchen": {"wet": 6},
        },
    }
    planner = _planner(seq, est)

    result = planner._estimate_timeline(
        dry_assign={"d1": ["Hall", "LivingRoom"], "d2": ["Bathroom", "Kitchen"]},
        wet_assign={"d3": ["Hall", "Bathroom", "LivingRoom", "Kitchen"]},
    )

    assert result["timeline"]["dry"] == {
        "Hall": 10, "LivingRoom": 22, "Bathroom": 8, "Kitchen": 17,
    }
    assert result["timeline"]["wet"] == {
        "Hall": 16, "Bathroom": 21, "LivingRoom": 29, "Kitchen": 35,
    }
    assert result["eta_min"] == 35


def test_room_missing_sequence_falls_back_to_end() -> None:
    """A room with no configured sequence position sorts after every
    sequenced room and is flagged in `unsequenced`, regardless of the order
    it was passed in (the assignment loop order is not a substitute)."""
    seq = {"Hall": 1}
    est = {"d1": {"Hall": {"dry": 10}, "Mystery": {"dry": 5}}}
    planner = _planner(seq, est)

    result = planner._estimate_timeline(
        dry_assign={"d1": ["Mystery", "Hall"]}, wet_assign={},
    )

    assert list(result["timeline"]["dry"].keys()) == ["Hall", "Mystery"]
    assert result["unsequenced"] == ["Mystery"]


def test_no_rooms_assigned_gives_zero_eta() -> None:
    """Unassigned/empty plans (e.g. every room came back unassigned) must not
    crash and must report a zero estimate rather than raising on max()."""
    planner = _planner({}, {})

    result = planner._estimate_timeline(dry_assign={}, wet_assign={})

    assert result["eta_min"] == 0
    assert result["timeline"] == {"dry": {}, "wet": {}}
    assert result["unsequenced"] == []


def test_default_room_min_used_when_no_estimate_exists() -> None:
    """A fresh install with no learned estimate yet falls back to
    DEFAULT_ROOM_MIN rather than treating the room as free."""
    planner = _planner({"Hall": 1}, {})

    result = planner._estimate_timeline(dry_assign={"d1": ["Hall"]}, wet_assign={})

    assert result["timeline"]["dry"]["Hall"] == DEFAULT_ROOM_MIN
