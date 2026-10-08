"""`anyvac.probe_map_rate` (docs/47 §4) — the measuring core, with a fake
map trait and a fake clock (no Home Assistant, no robot)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.anyvac.services import _probe_map_rate


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def _pts(n: int) -> Any:
    return SimpleNamespace(path=[[SimpleNamespace(x=i, y=0) for i in range(n)]])


class _Trait:
    """A map that only changes on every 3rd fetch (robot map updated ~3x
    slower than we fetch); each fetch costs 0.5 s."""

    def __init__(self, clock: _Clock, fail_at: set[int] = frozenset()) -> None:
        self.clock = clock
        self.calls = 0
        self.fail_at = fail_at
        self.raw_api_response: bytes | None = None
        self.map_data: Any = None

    async def refresh(self) -> None:
        self.calls += 1
        self.clock.t += 0.5
        if self.calls in self.fail_at:
            raise TimeoutError("no answer")
        gen = (self.calls - 1) // 3
        self.raw_api_response = b"map-%d" % gen
        self.map_data = SimpleNamespace(
            vacuum_position=SimpleNamespace(x=100 * gen, y=0),
            path=_pts(10 * gen),
            mop_path=None,
            additional_parameters={"map_sequence": gen},
        )


@pytest.mark.asyncio
async def test_reports_changes_latency_and_spacing() -> None:
    clock = _Clock()
    trait = _Trait(clock)
    r = await _probe_map_rate(trait, duration_s=60, interval_s=5, clock=clock, sleep=clock.sleep)
    assert r["fetches"] == 13  # t = 0, 5, ..., 60
    assert r["errors"] == 0
    assert r["changed"] == 4  # generations 1..4 appear at fetch 4, 7, 10, 13
    assert r["latency_ms"] == {"min": 500, "median": 500, "max": 500}
    assert r["seconds_between_changes"] == {"min": 15.0, "median": 15.0, "max": 15.0}
    assert r["new_path_points_per_change"] == {"min": 10, "median": 10, "max": 10}
    assert r["moved_mm_per_change"] == {"min": 100, "median": 100, "max": 100}
    first, fourth = r["samples"][0], r["samples"][3]
    assert "changed" not in first and first["bytes"] == len(b"map-0")
    assert fourth["changed"] is True and fourth["map_sequence"] == 1
    assert "raw" not in fourth  # bytes never leave the probe


@pytest.mark.asyncio
async def test_a_failed_fetch_is_reported_not_raised() -> None:
    clock = _Clock()
    trait = _Trait(clock, fail_at={2})
    r = await _probe_map_rate(trait, duration_s=10, interval_s=5, clock=clock, sleep=clock.sleep)
    assert r["fetches"] == 3
    assert r["errors"] == 1
    assert "TimeoutError" in r["samples"][1]["error"]


@pytest.mark.asyncio
async def test_dynamic_diff_shape_is_reported() -> None:
    clock = _Clock()
    trait = _Trait(clock)

    async def diff() -> bytes:
        return b"\x01\x02" * 20

    r = await _probe_map_rate(
        trait, duration_s=10, interval_s=5, dynamic_diff=diff, clock=clock, sleep=clock.sleep
    )
    d = r["samples"][0]["dynamic_diff"]
    assert d["type"] == "bytes" and d["bytes"] == 40 and d["head"] == ("0102" * 16)


@pytest.mark.asyncio
async def test_dynamic_diff_json_answer_is_returned_whole_and_via_is_reported() -> None:
    clock = _Clock()
    trait = _Trait(clock)
    trait.via = "cloud"
    answer = {"diff": {"1": {"count": 738}, "43": {"max_len": 0, "x": list(range(50))}}}

    async def diff() -> Any:
        return answer

    r = await _probe_map_rate(
        trait, duration_s=5, interval_s=5, dynamic_diff=diff, clock=clock, sleep=clock.sleep
    )
    s = r["samples"][0]
    assert s["dynamic_diff"]["value"] == answer
    assert s["via"] == "cloud"
