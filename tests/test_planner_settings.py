"""Tests for per-vacuum clean settings (2026-07-26).

Diagnosed from a field report: S7 had `fan_speed: max_plus` configured in its
own setting preset, but always ran at `max` instead. Root cause was that
`settings` was a single object SHARED by every vacuum doing a given pass —
``build_tasks()`` read ``settings.get("dry")`` once and reused it for every
dry-capable robot, so whichever vacuum's preset the card happened to pick
(effectively the first dry-capable entry in its config) silently won for all
of them, S6's fan_speed overriding S7's.

``CleanPlanner._settings_for_duid()`` resolves one vacuum's settings from the
new per-vacuum ``{kind: {vacuum_ref: {...}}}`` shape — these tests lock down
both the resolution itself and the end-to-end effect: two dry-capable robots
given different fan_speed settings in the same ``build_tasks()`` call must
each keep their own value.

Like `test_planner_pin.py`, the planner is built via `object.__new__` to skip
`CleanPlanner.__init__`.
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


def _planner(monkeypatch: pytest.MonkeyPatch) -> CleanPlanner:
    """Two dry-only robots, each the exclusive owner of its own room, so
    assignment is deterministic without needing a pin — mirrors the field
    fleet's dry-only S6 + S7."""
    monkeypatch.setattr(
        "custom_components.anyvac.planner.selects_for_duid",
        lambda hass, duid: {},
    )
    devices = {
        "d1": _FakeDevice(),  # no mop_signal -> dry-only
        "d2": _FakeDevice(),
    }
    segments = {
        "d1": {"Bedroom": 1},
        "d2": {"Kitchen": 2},
    }
    entity_of = {"d1": "vacuum.s6_kitchen", "d2": "vacuum.s7_maxv"}
    planner = object.__new__(CleanPlanner)
    planner.hass = None
    planner.coord = _FakeCoordinator(
        room_sequence={"Bedroom": 1, "Kitchen": 2},
        rooms_estimate={},
    )
    planner.devices = devices
    planner.segments = segments
    planner.entity_of = entity_of
    planner.duid_of_entity = {v: k for k, v in entity_of.items() if v}
    return planner


def test_settings_for_duid_resolves_by_entity_or_duid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _planner(monkeypatch)
    settings = {
        "dry": {
            "vacuum.s6_kitchen": {"fan_speed": "max_plus"},
            "d2": {"fan_speed": "quiet"},  # duid form also accepted
        }
    }
    assert planner._settings_for_duid(settings, "dry", "d1") == {
        "fan_speed": "max_plus"
    }
    assert planner._settings_for_duid(settings, "dry", "d2") == {
        "fan_speed": "quiet"
    }


def test_settings_for_duid_handles_missing_and_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _planner(monkeypatch)
    assert planner._settings_for_duid(None, "dry", "d1") == {}
    assert planner._settings_for_duid({}, "dry", "d1") == {}
    assert planner._settings_for_duid({"dry": {}}, "dry", "d1") == {}
    # A vacuum with no entry of its own gets nothing (firmware defaults),
    # not another vacuum's settings.
    assert planner._settings_for_duid(
        {"dry": {"vacuum.s7_maxv": {"fan_speed": "quiet"}}}, "dry", "d1"
    ) == {}


def test_two_dry_vacuums_keep_own_fan_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact scenario from the field report: two dry-capable robots, each
    with its own configured fan_speed, cleaning in the same `build_tasks()`
    call. Each task must carry ITS OWN vacuum's fan_speed, not one shared
    across both."""
    planner = _planner(monkeypatch)
    tasks, plan = planner.build_tasks(
        ["Bedroom", "Kitchen"],
        "dry",
        settings={
            "dry": {
                "vacuum.s6_kitchen": {"fan_speed": "quiet"},
                "vacuum.s7_maxv": {"fan_speed": "max_plus"},
            }
        },
    )
    assert plan["dry"] == {
        "vacuum.s6_kitchen": ["Bedroom"],
        "vacuum.s7_maxv": ["Kitchen"],
    }
    by_vacuum = {t["vacuum"]: t for t in tasks}
    assert by_vacuum["vacuum.s6_kitchen"]["fan_speed"] == "quiet"
    assert by_vacuum["vacuum.s7_maxv"]["fan_speed"] == "max_plus"


def test_vacuum_with_no_settings_entry_gets_no_fan_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vacuum assigned to a pass but absent from `settings` must not
    silently inherit another vacuum's value — it should run with no
    fan_speed override (firmware default) instead."""
    planner = _planner(monkeypatch)
    tasks, _plan = planner.build_tasks(
        ["Bedroom", "Kitchen"],
        "dry",
        settings={"dry": {"vacuum.s7_maxv": {"fan_speed": "max_plus"}}},
    )
    by_vacuum = {t["vacuum"]: t for t in tasks}
    assert by_vacuum["vacuum.s6_kitchen"]["fan_speed"] is None
    assert by_vacuum["vacuum.s7_maxv"]["fan_speed"] == "max_plus"
