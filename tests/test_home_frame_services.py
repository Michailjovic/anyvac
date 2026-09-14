"""Tests for docs/40 Fáze 2.1 — `frame: "home"` targeting on `goto`/
`zone_clean` (`custom_components/anyvac/services.py`).

Like `test_dump_raw_map.py`, the real HA service handlers (closures inside
`async_register_services`, doing a real `hass.services.async_call`) are not
exercised here — what's tested is what was factored OUT of them precisely so
it is testable without a running Home Assistant instance: `_target_mm_for`
(goto's one point) and `_target_zone_mm_for` (zone_clean's two corners),
against a minimal fake `hass`/coordinator exposing only `pct_to_mm`/
`home_px_to_mm` (docs/14 rule 1 — no second implementation of coordinate
resolution to mock against).
"""

from __future__ import annotations

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.anyvac.services import (
    _target_mm_for,
    _target_zone_mm_for,
)


class _FakeCall:
    """Stands in for a `homeassistant.core.ServiceCall` — only `.data` (a
    plain dict, since `_target_mm_for`/`_target_zone_mm_for` only ever use
    `in`/`.get` on it) is read."""

    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeCoord:
    """Stands in for an AnyVacCoordinator exposing only the two conversion
    methods `_mm_for`/`_home_mm_for` call. `pct_to_mm`/`home_px_to_mm` return
    `None` for an unknown duid — the SAME "unknown to this coordinator,
    maybe another one has it" contract the real coordinator methods use."""

    def __init__(self, duid: str, *, has_home: bool = True) -> None:
        self._duid = duid
        self._has_home = has_home

    def pct_to_mm(self, duid: str, x_pct: float, y_pct: float):
        if duid != self._duid:
            return None
        return (round(x_pct * 100), round(y_pct * 100))  # arbitrary but deterministic

    def home_px_to_mm(self, duid: str, x: float, y: float):
        if duid != self._duid or not self._has_home:
            return None
        return (x * 10.0 + 1.0, y * 10.0 + 2.0)  # arbitrary but deterministic + non-integer


class _FakeEntry:
    def __init__(self, coord) -> None:
        self.runtime_data = coord


class _FakeConfigEntries:
    def __init__(self, coords: list) -> None:
        self._entries = [_FakeEntry(c) for c in coords]

    def async_entries(self, domain: str):  # noqa: ARG002 - matches real signature
        return self._entries


class _FakeHass:
    def __init__(self, coords: list) -> None:
        self.config_entries = _FakeConfigEntries(coords)


def _hass_for(duid: str, *, has_home: bool = True) -> _FakeHass:
    return _FakeHass([_FakeCoord(duid, has_home=has_home)])


# ── _target_mm_for (goto) ────────────────────────────────────────────────────


def test_default_frame_uses_pct_to_mm() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"x_pct": 10.0, "y_pct": 20.0})
    assert _target_mm_for(hass, "s6", call) == (1000, 2000)


def test_explicit_frame_robot_uses_pct_to_mm() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"frame": "robot", "x_pct": 10.0, "y_pct": 20.0})
    assert _target_mm_for(hass, "s6", call) == (1000, 2000)


def test_frame_home_uses_home_px_to_mm_and_rounds_to_int() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"frame": "home", "x_home_px": 3.0, "y_home_px": 4.0})
    # home_px_to_mm(3, 4) -> (31.0, 42.0) per the fake above.
    assert _target_mm_for(hass, "s6", call) == (31, 42)


def test_frame_home_missing_coordinates_raises() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"frame": "home", "x_home_px": 3.0})  # y_home_px missing
    with pytest.raises(HomeAssistantError, match="x_home_px and y_home_px"):
        _target_mm_for(hass, "s6", call)


def test_default_frame_missing_pct_raises() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"x_pct": 10.0})  # y_pct missing
    with pytest.raises(HomeAssistantError, match="x_pct/y_pct"):
        _target_mm_for(hass, "s6", call)


def test_frame_home_without_registration_raises_a_specific_error() -> None:
    """A vacuum with no home-frame registration must fail with a message
    that points at WHY (not just "no coordinator answered") — the whole
    point of Fáze 2's frame: "home" input is that it's conditional on Fáze
    1's registration having actually succeeded for this vacuum."""
    hass = _hass_for("s6", has_home=False)
    call = _FakeCall({"frame": "home", "x_home_px": 1.0, "y_home_px": 1.0})
    with pytest.raises(HomeAssistantError, match="no home-frame registration"):
        _target_mm_for(hass, "s6", call)


def test_unknown_duid_falls_through_every_coordinator() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"x_pct": 10.0, "y_pct": 20.0})
    with pytest.raises(HomeAssistantError):
        _target_mm_for(hass, "s7", call)


# ── _target_zone_mm_for (zone_clean) ─────────────────────────────────────────


def test_zone_default_frame_resolves_both_corners() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"x1_pct": 1.0, "y1_pct": 2.0, "x2_pct": 3.0, "y2_pct": 4.0})
    a, b = _target_zone_mm_for(hass, "s6", call)
    assert a == (100, 200)
    assert b == (300, 400)


def test_zone_frame_home_resolves_both_corners() -> None:
    hass = _hass_for("s6")
    call = _FakeCall(
        {
            "frame": "home",
            "x1_home_px": 1.0,
            "y1_home_px": 2.0,
            "x2_home_px": 3.0,
            "y2_home_px": 4.0,
        }
    )
    a, b = _target_zone_mm_for(hass, "s6", call)
    assert a == (11, 22)
    assert b == (31, 42)


def test_zone_frame_home_missing_one_corner_field_raises() -> None:
    hass = _hass_for("s6")
    call = _FakeCall(
        {"frame": "home", "x1_home_px": 1.0, "y1_home_px": 2.0, "x2_home_px": 3.0}
    )  # y2_home_px missing
    with pytest.raises(HomeAssistantError, match="x1_home_px/y1_home_px"):
        _target_zone_mm_for(hass, "s6", call)


def test_zone_default_frame_missing_corner_raises() -> None:
    hass = _hass_for("s6")
    call = _FakeCall({"x1_pct": 1.0, "y1_pct": 2.0, "x2_pct": 3.0})  # y2_pct missing
    with pytest.raises(HomeAssistantError, match="x1_pct/y1_pct"):
        _target_zone_mm_for(hass, "s6", call)
