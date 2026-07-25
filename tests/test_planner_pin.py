"""Tests for per-kind room pins (docs/18 §7e, per-kind since 2026-07-25).

Diagnosed from a real HA log: `room_pins` used to be a single flat
`{room: vacuum}` value, so pinning a room's dry pass and its wet pass to
different vacuums (unavoidable on a fleet with no dual-capable robot, e.g.
dry-only S6/S7 + wet-only S8) silently clobbered whichever was set first.
The planner then hit its own defensive fallback — and logged a WARNING —
for the now-mismatched pass on every `plan`/`clean` call for that room.

`CleanPlanner._pin_for_kind()` flattens the new per-room `{"dry"/"wet":
vacuum}` map down to a single kind's flat `{room: vacuum}` before calling
`assign()`, which is otherwise unchanged — these tests lock down both the
flattening itself and the end-to-end effect: a correctly-pinned split fleet
must assign cleanly with no fallback, while a genuinely mismatched pin (the
defensive case `assign()` still needs to handle, e.g. a stale/manual
`anyvac.clean` call with a bad `pin` override) must still fall back with a
warning exactly as before.

Like `test_planner_pool_tasks.py`, the planner is built via
`object.__new__` to skip `CleanPlanner.__init__`.
"""

from __future__ import annotations

import logging

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
    """Two rooms, one dry-only robot (d1) and one wet-only robot (w1) —
    mirrors the field-reported fleet (dry-only S6/S7 + wet-only S8)."""
    monkeypatch.setattr(
        "custom_components.anyvac.planner.selects_for_duid",
        lambda hass, duid: {},
    )
    devices = {
        "d1": _FakeDevice(),  # no mop_signal -> dry-only
        "w1": _FakeDevice(mop_signal={"water_box_mode": 200}),
    }
    segments = {
        "d1": {"Bedroom": 1, "Kitchen": 2},
        "w1": {"Bedroom": 1, "Kitchen": 2},
    }
    entity_of = {"d1": "vacuum.s6_kitchen", "w1": "vacuum.s8_maxv"}
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


def test_pin_for_kind_flattens_and_filters() -> None:
    pin = {
        "Bedroom": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"},
        "Kitchen": {"dry": "vacuum.s6_kitchen"},  # no wet entry
        "Hall": {},  # empty -> nothing to flatten
    }
    assert CleanPlanner._pin_for_kind(pin, "dry") == {
        "Bedroom": "vacuum.s6_kitchen",
        "Kitchen": "vacuum.s6_kitchen",
    }
    assert CleanPlanner._pin_for_kind(pin, "wet") == {"Bedroom": "vacuum.s8_maxv"}


def test_pin_for_kind_handles_missing_and_none() -> None:
    assert CleanPlanner._pin_for_kind(None, "dry") == {}
    assert CleanPlanner._pin_for_kind({}, "dry") == {}


def test_split_fleet_pin_assigns_both_passes_without_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The exact scenario from the field report: Bedroom pinned dry->S6,
    wet->S8 in a single per-kind pin. A "both" job must honor BOTH pins —
    no unassigned rooms, and critically no "not applicable" fallback
    WARNING, since each pin is now only ever looked up for its own kind."""
    planner = _planner(monkeypatch)
    with caplog.at_level(logging.WARNING):
        tasks, plan = planner.build_tasks(
            ["Bedroom", "Kitchen"],
            "both",
            pin={
                "Bedroom": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"},
                "Kitchen": {"dry": "vacuum.s6_kitchen", "wet": "vacuum.s8_maxv"},
            },
        )
    assert "unassigned" not in plan  # popped when empty (nothing fell back)
    assert plan["dry"] == {"vacuum.s6_kitchen": ["Bedroom", "Kitchen"]}
    assert plan["wet"] == {"vacuum.s8_maxv": ["Bedroom", "Kitchen"]}
    assert "not applicable" not in caplog.text
    assert tasks  # sanity: something was actually built


def test_pin_incapable_of_kind_still_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive case `assign()` must still handle: a pin that names a
    vacuum genuinely incapable of the requested kind (e.g. a stale/manual
    override) falls back to automatic assignment and logs a warning, same
    as before this change — this class of pin is no longer reachable from
    the card's own UI (candidates are kind-filtered, docs/18 §7e), but
    `anyvac.clean`'s `pin` override is a public service field any automation
    can call directly."""
    planner = _planner(monkeypatch)
    with caplog.at_level(logging.WARNING):
        dry_assign, dry_un = planner.assign(
            ["Bedroom"], "wet", None, {"Bedroom": "vacuum.s6_kitchen"}
        )
    assert dry_un == []
    assert dry_assign == {"w1": ["Bedroom"]}  # fell back to the only wet-capable robot
    assert "not applicable for wet pass" in caplog.text
