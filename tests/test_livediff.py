"""docs/48 — live position from the robot's local get_dynamic_map_diff.

The decoder is checked against REAL answers recorded from the user's S8 MaxV
Ultra on 2026-10-08 (docs/47 §4.3); the full map fetched in the same instant
reported the robot at (23983, 24317) and 870 → 875 path points.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.anyvac import livediff, localprobe
from custom_components.anyvac import coordinator as coordinator_mod
from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice

T5 = "CAAIAAwAAACvXQAA/V4AAFoAAAADABQAFAAAAAUAAAAEAAAAAAAAALVdC127XQldtV11XbFdPl6vXf1eEgAIAAUAAAAFBQUFBQ=="
T10 = "CAAIAAwAAAD9XQAABl8AAKP///8DABQAFAAAAAUAAAAEAAAAAAAAAKxddl+rXahfv12pX/pdm1/9XQZfEgAIAAUAAAAFBQUFBQ=="
T50 = "CAAIAAwAAABaYAAAVl4AABoAAAADABQAEAAAAAQAAAAEAAAAAAAAAH5fKl5wX+Rdjl/xXVpgVl4SAAgABAAAAAUMDAw="
T55 = "CAAIAAwAAAAZZAAAXmAAAFAAAAADABQAFAAAAAUAAAAEAAAAAAAAAGlh116KYmJfUWPBXwBkDmAZZF5gEgAIAAUAAAAMDAwMDA=="


def _answer(start: int | None, data: str | None, n: int = 5) -> dict[str, Any]:
    block: dict[str, Any] = {"max_len": (start or 0) + n, "nonce": 0, "count": 0}
    if data is not None:
        block.update(start=start, len=n, data=data)
    return {"diff": {"2": {"obstacle": 3038, "space": 20290, "count": 23328}, "3": block,
                     "26": {"count": 26}}, "nonce": 0, "result": 2}


def test_real_answer_decodes_to_the_full_maps_position_and_points() -> None:
    p = livediff.parse_diff(_answer(870, T5))
    assert p["start"] == 870
    assert p["pos"] == {"x": 23983, "y": 24317, "a": 90}
    assert len(p["points"]) == 5 and p["points"][0] == (23989, 23819)
    assert p["points"][-1] == (23983, 24317)  # the trace ends where the robot is
    assert p["flags"] == [5, 5, 5, 5, 5]


def test_negative_heading_wraps_like_the_library_parser() -> None:
    p = livediff.parse_diff(_answer(875, T10))
    assert p["pos"]["a"] == -93  # 0xffffffa3


def test_mop_flag_changes_inside_one_answer() -> None:
    p = livediff.parse_diff(_answer(915, T50, n=4))
    assert len(p["points"]) == 4
    assert p["flags"] == [5, 12, 12, 12]


def test_answer_without_new_points() -> None:
    p = livediff.parse_diff(_answer(None, None))
    assert p == {"start": None, "pos": None, "points": [], "flags": []}
    assert livediff.parse_diff({"result": 2}) is None
    assert livediff.parse_diff(["ok"]) is None


def test_trail_appends_in_order_and_skips_overlap() -> None:
    t = livediff.LiveTrail(base=870)
    assert t.apply(livediff.parse_diff(_answer(870, T5)))
    assert t.next == 875 and len(t.points) == 5
    # The same answer again (overlap): nothing new on the trace.
    t.apply(livediff.parse_diff(_answer(870, T5)))
    assert t.next == 875 and len(t.points) == 5
    t.apply(livediff.parse_diff(_answer(875, T10)))
    assert t.next == 880 and len(t.points) == 10 and not t.broken


def test_trail_gap_stops_the_trace_but_keeps_the_position() -> None:
    t = livediff.LiveTrail(base=870)
    t.apply(livediff.parse_diff(_answer(870, T5)))
    t.apply(livediff.parse_diff(_answer(919, T55)))  # 875..918 consumed by someone else
    assert t.broken and len(t.points) == 5
    assert t.pos["x"] == 25625  # position still follows


def test_wet_segments_follow_the_mop_flag() -> None:
    t = livediff.LiveTrail(base=0)
    t.apply({"start": 0, "pos": None, "points": [(1, 1), (2, 2), (3, 3), (4, 4)], "flags": [0, 12, 12, 0]})
    assert t.wet_segments() == [[{"x": 2, "y": 2}, {"x": 3, "y": 3}]]
    assert not t.wet_starts_on_first_point()
    assert len(t.dry_segment()) == 4


# --- coordinator side ---------------------------------------------------------

CALIB = [  # 1 px per 100 mm, no flip — enough to check the transform is applied
    {"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}},
    {"vacuum": {"x": 1000, "y": 0}, "map": {"x": 10, "y": 0}},
    {"vacuum": {"x": 0, "y": 1000}, "map": {"x": 0, "y": 10}},
]


def _coord(path_points: int = 870, dry_open: bool = True, wet_open: bool = True) -> AnyVacCoordinator:
    c = object.__new__(AnyVacCoordinator)
    c.data = {"d1": AnyVacDevice(duid="d1", slug="d1", name="S8", data={
        "path_points": path_points, "calibration_points": CALIB, "in_cleaning": True})}
    c._dry_path_open = {"d1": dry_open}
    c._wet_path_open = {"d1": wet_open}
    c._robot_frame, c._home_frames = {}, {}
    c._live, c._live_pub = {}, {}
    c._live_inflight, c._live_fails, c._live_backoff = set(), {}, {}
    c.updates = 0

    def _upd() -> None:
        c.updates += 1

    c.async_update_listeners = _upd  # type: ignore[method-assign]
    return c


def test_payload_is_in_px_and_tied_to_its_snapshot() -> None:
    c = _coord()
    c._apply_live("d1", livediff.parse_diff(_answer(870, T5)))
    live = c.live_for("d1")
    assert c.updates == 1
    assert live["base_points"] == 870
    assert live["pos_px"] == {"x": 239.8, "y": 243.2, "a": 90}
    assert live["dry_continues"] is True and len(live["dry_px"][0]) == 5
    assert live["wet_continues"] is True and len(live["wet_px"]) == 1
    assert live["pos_home_px"] is None and live["dry_home_px"] == []  # no home frame


def test_closed_dry_gate_publishes_no_dry_tail() -> None:
    c = _coord(dry_open=False)
    c._apply_live("d1", livediff.parse_diff(_answer(870, T5)))
    assert c.live_for("d1")["dry_px"] == [] and c.live_for("d1")["dry_continues"] is False


def test_new_snapshot_base_starts_a_new_trail() -> None:
    c = _coord()
    c._apply_live("d1", livediff.parse_diff(_answer(870, T5)))
    c.data["d1"].data["path_points"] = 880  # a newer full map arrived
    c._apply_live("d1", livediff.parse_diff(_answer(880, T5)))
    assert c.live_for("d1")["base_points"] == 880
    assert c._live["d1"].next == 885


@pytest.mark.asyncio
async def test_fetch_failures_back_off(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord()
    v1 = type("V1", (), {"is_local_connected": True})()
    c.roborock_coordinator_for = lambda duid: object()  # type: ignore[method-assign]
    monkeypatch.setattr(localprobe, "v1_channel_of", lambda rb: v1)
    calls: list[dict[str, Any]] = []

    async def no_answer(v1ch: Any, method: str, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"ack": None}

    monkeypatch.setattr(localprobe, "local_request", no_answer)
    for _ in range(3):
        c._live_inflight.add("d1")
        await c._live_fetch("d1")
    assert calls[0]["watch_cloud"] is False
    assert c._live_backoff["d1"] > 0 and "d1" not in c._live_inflight

    class _Hass:
        def __init__(self) -> None:
            self.tasks = 0

        def async_create_background_task(self, coro: Any, name: str) -> None:
            self.tasks += 1
            coro.close()

    c.hass = _Hass()
    c._live_tick()
    assert c.hass.tasks == 0  # backing off
    c._live_backoff["d1"] = 0
    c._live_tick()
    assert c.hass.tasks == 1
    c._live_tick()
    assert c.hass.tasks == 1  # one in flight per robot


@pytest.mark.asyncio
async def test_good_answer_resets_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord()
    v1 = type("V1", (), {"is_local_connected": True})()
    c.roborock_coordinator_for = lambda duid: object()  # type: ignore[method-assign]
    monkeypatch.setattr(localprobe, "v1_channel_of", lambda rb: v1)

    async def answer(v1ch: Any, method: str, **kw: Any) -> dict[str, Any]:
        assert method == "get_dynamic_map_diff"
        return {"ack": _answer(870, T5)}

    monkeypatch.setattr(localprobe, "local_request", answer)
    c._live_fails["d1"] = 2
    await c._live_fetch("d1")
    assert c._live_fails["d1"] == 0 and c.live_for("d1")["seq"] == 1


@pytest.mark.asyncio
async def test_live_stats_count_what_the_diff_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord()
    c._live_stats = {}
    v1 = type("V1", (), {"is_local_connected": True})()
    c.roborock_coordinator_for = lambda duid: object()  # type: ignore[method-assign]
    monkeypatch.setattr(localprobe, "v1_channel_of", lambda rb: v1)
    answers = iter([_answer(870, T5), _answer(None, None), None])

    async def answer(v1ch: Any, method: str, **kw: Any) -> dict[str, Any]:
        return {"ack": next(answers), "latency_ms": 20}

    monkeypatch.setattr(localprobe, "local_request", answer)
    for _ in range(3):
        await c._live_fetch("d1")
    st = c.live_stats_for("d1")
    assert (st["with_points"], st["empty"], st["no_answer"]) == (1, 1, 1)
    assert st["last"]["what"] == "no_answer"
    assert c.updates == 2  # every parsed answer republishes (stats changed)
