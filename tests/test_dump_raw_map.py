"""Tests for `anyvac.dump_raw_map` (docs/40 Fáze 0, 2026-09-13).

DEBUG/DIAGNOSTIC ONLY service: writes a vacuum's raw Roborock map bytes to
config/www/anyvac/debug/ so they can be pulled into `anyvac/tools/samples/`
for the offline home-frame registration probe (docs/40 §3.2). No behaviour
change to anything else, no card involvement.

Like `test_snapshot_floorplan.py`, the actual HA service handler (a closure
inside `async_register_services`, doing real file I/O) is not exercised here
— what's tested is what was factored OUT of it precisely so it is testable
without a running Home Assistant instance: the filename logic, and the "no
raw data available yet" error path (`_require_raw_map`, mocked coordinators
exposing only `raw_map_for`, per docs/14 rule 1 — no second implementation of
the piggyback walk to mock against).
"""

from __future__ import annotations

import pytest

from homeassistant.exceptions import ServiceValidationError

from custom_components.anyvac.services import _raw_map_filename, _require_raw_map


# ── _raw_map_filename ──────────────────────────────────────────────────────────


def test_filename_is_slugified_with_map_flag_suffix() -> None:
    assert _raw_map_filename("S6 MaxV Ultra!", 0) == "s6_maxv_ultra_0.bin"
    assert _raw_map_filename("s6_kitchen_map_0", 3) == "s6_kitchen_map_0_3.bin"


def test_filename_is_always_bin() -> None:
    assert _raw_map_filename("S8", 1).endswith(".bin")


def test_filename_falls_back_when_name_or_flag_is_empty() -> None:
    assert _raw_map_filename("", 0) == "vacuum_0.bin"
    assert _raw_map_filename("!!!", 0) == "vacuum_0.bin"
    assert _raw_map_filename("s6", None) == "s6_map.bin"


def test_filename_is_stable_for_the_same_inputs() -> None:
    # Re-dumping the same vacuum/map must produce the same filename (so a
    # repeated dump overwrites rather than accumulating stray files).
    assert _raw_map_filename("S8 MaxV Ultra", 1) == _raw_map_filename("S8 MaxV Ultra", 1)


# ── _require_raw_map ────────────────────────────────────────────────────────────


class _FakeCoordNoRaw:
    """Stands in for an AnyVacCoordinator that has no raw bytes for anyone."""

    def raw_map_for(self, duid: str):  # noqa: ARG002
        return None


class _FakeCoordWithRaw:
    """Stands in for an AnyVacCoordinator that DOES have raw bytes for one duid."""

    def __init__(self, duid: str, raw: bytes, meta: dict) -> None:
        self._duid = duid
        self._raw = raw
        self._meta = meta

    def raw_map_for(self, duid: str):
        if duid != self._duid:
            return None
        return self._raw, self._meta


def test_require_raw_map_raises_when_no_coordinator_has_raw_bytes() -> None:
    with pytest.raises(ServiceValidationError, match="no raw map bytes available"):
        _require_raw_map([_FakeCoordNoRaw(), _FakeCoordNoRaw()], "s6")


def test_require_raw_map_raises_for_unknown_duid() -> None:
    meta = {"map_flag": 0, "map_index": 1, "map_sequence": 2}
    coords = [_FakeCoordWithRaw("s6", b"abc", meta)]
    with pytest.raises(ServiceValidationError):
        _require_raw_map(coords, "s7")


def test_require_raw_map_returns_first_match_across_coordinators() -> None:
    meta = {"map_flag": 0, "map_index": 5, "map_sequence": 9}
    coords = [_FakeCoordNoRaw(), _FakeCoordWithRaw("s6", b"abc", meta)]
    raw, got_meta = _require_raw_map(coords, "s6")
    assert raw == b"abc"
    assert got_meta == meta
