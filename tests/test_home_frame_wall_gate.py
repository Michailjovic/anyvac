"""1.57.1 field fixes for the home frame (docs/40), found on 2026-11-24.

1. A one-room cellar (S7 "bunker", 68 m2, one open rectangle) registered onto
   the 53 m2 flat at covered 0.72 / IoU 0.67 — past the floor-only gate — and
   was merged into the flat's frame. `wall_conflict` adds a structural gate:
   the flat's interior walls lie in the middle of the cellar's open floor.
2. Three robots of the same flat ended up in three private frames: their
   registrations ran concurrently on a fresh start, each saw no frames, and
   continuity then kept each in its own frame for good. A robot alone in its
   frame now joins a shared frame that passes the gate.

Real maps are the user's floor plan and stay out of the repository; the
synthetic flat below has the same features (a long strip split by interior
walls with doorways) at a smaller size.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from custom_components.anyvac import homeframe
from custom_components.anyvac.coordinator import AnyVacCoordinator, _compute_home_frame_result

from ._synthetic_raw_map import make_raw_map

NOW = "2026-11-24T00:00:00+00:00"
W, H = 200, 60


def _flat_walls() -> list[tuple[int, int, int, int]]:
    """Interior walls of a 200 x 60 cell strip: cross walls with doorways and
    a long wall through the middle, deliberately not symmetric."""
    walls = []
    for x in (35, 70, 120, 160):
        walls += [(x, 1, x + 1, 22), (x, 28, x + 1, 59)]  # doorway rows 22..28
    walls += [(1, 32, 60, 33), (75, 30, 115, 31), (130, 34, 199, 35)]
    return walls


def _flat(rotate90: bool = False, half: bool = False, top: int = 0, left: int = 0) -> bytes:
    rooms = {1: (1, 1, W - 1, H - 1)}
    walls = _flat_walls()
    if half:  # a robot that only explored the left half so far
        rooms = {1: (1, 1, W // 2, H - 1)}
        walls = [w for w in walls if w[0] < W // 2] + [(W // 2, 1, W // 2 + 1, H - 1)]
    if not rotate90:
        return make_raw_map(W if not half else W // 2 + 2, H, top=top, left=left, rooms=rooms, walls=walls)
    # 90 deg: (x, y) -> (y, W - x)
    rr = {k: (y0, W - x1, y1, W - x0) for k, (x0, y0, x1, y1) in rooms.items()}
    ww = [(y0, W - x1, y1, W - x0) for (x0, y0, x1, y1) in walls]
    return make_raw_map(H, W, top=top, left=left, rooms=rr, walls=ww)


def _open_room() -> bytes:
    """The cellar: one open room with the strip's outline, slightly larger."""
    return make_raw_map(W + 10, H + 8, top=300, left=-40, rooms={1: (1, 1, W + 9, H + 7)})


def _found(duid: str, raw: bytes) -> tuple[dict[str, Any], dict[str, str]]:
    res = _compute_home_frame_result({}, {}, duid, raw, 1, 1, "x", {}, NOW)
    fid = res["robot_frame"]
    return {fid: res["updated_frames"][fid]}, {duid: fid}


def test_wall_conflict_separates_same_place_from_a_lookalike() -> None:
    frames, rf = _found("s6", _flat())
    frame = next(iter(frames.values()))
    for raw, same in ((_flat(rotate90=True, top=40, left=90), True), (_flat(half=True), True), (_open_room(), False)):
        g = homeframe.decode_grid(raw)
        reg = homeframe.register_with_fallback(g.floor, frame["floor_mask"])
        assert reg.covered >= homeframe.DEFAULT_GATE_COVERED and reg.iou >= homeframe.DEFAULT_GATE_IOU
        conflict = homeframe.wall_conflict(frame, g, *homeframe.affine_from_registration(reg, g, frame["origin_mm"]))
        if same:
            assert conflict <= homeframe.DEFAULT_GATE_WALL_CONFLICT, conflict
        else:
            assert conflict > homeframe.DEFAULT_GATE_WALL_CONFLICT, conflict


def test_lookalike_place_gets_its_own_frame_even_though_the_floor_matches() -> None:
    frames, rf = _found("s6", _flat())
    res = _compute_home_frame_result(frames, rf, "bunker", _open_room(), 1, 1, "b", {}, NOW)
    assert res["status"] == "unaligned"
    assert res["robot_frame"] != rf["s6"]


def test_same_flat_rotated_and_partial_still_aligns() -> None:
    frames, rf = _found("s6", _flat())
    for duid, raw in (("s8", _flat(rotate90=True, top=40, left=90)), ("s7", _flat(half=True))):
        res = _compute_home_frame_result(frames, rf, duid, raw, 1, 1, duid, {}, NOW)
        assert res["status"] == "aligned", duid
        assert res["robot_frame"] == rf["s6"]
        assert res["wall_conflict"] <= homeframe.DEFAULT_GATE_WALL_CONFLICT


def test_robot_alone_in_its_frame_joins_the_shared_frame() -> None:
    """The field state: two robots of one flat each founded a frame (registered
    concurrently on a fresh start). The next registration moves the second one
    into the first's frame instead of keeping it in its private one."""
    f6, rf6 = _found("s6", _flat())
    f8, rf8 = _found("s8", _flat(rotate90=True, top=40, left=90))
    # from s8's side, s6's frame holds another robot; s8's own holds only s8
    frames = {**f6, **f8}
    robot_frame = {**rf6, **rf8}
    res = _compute_home_frame_result(frames, robot_frame, "s8", _flat(rotate90=True, top=40, left=90), 1, 1, "s8", {}, NOW)
    assert res["robot_frame"] == rf6["s6"]
    assert res["status"] == "aligned"
    assert rf8["s8"] in res["removed_robot_from_frames"]


def test_robot_alone_with_nothing_else_matching_stays_put() -> None:
    f6, rf6 = _found("s6", _flat())
    fb, rfb = _found("bunker", _open_room())
    res = _compute_home_frame_result({**f6, **fb}, {**rf6, **rfb}, "bunker", _open_room(), 1, 1, "b", {}, NOW)
    assert res["robot_frame"] == rfb["bunker"]
    assert res["status"] == "reference"


@pytest.mark.asyncio
async def test_left_empty_frame_is_marked_stale() -> None:
    coord = object.__new__(AnyVacCoordinator)
    f6, rf6 = _found("s6", _flat())
    f8, rf8 = _found("s8", _flat(rotate90=True, top=40, left=90))
    coord._home_frames = {**f6, **f8}
    coord._robot_frame = {**rf6, **rf8}
    coord._home_frame_inflight, coord._home_frame_disabled = set(), set()
    coord._home_frame_cache_key, coord._home_frame_room_masks = {}, {}
    coord._delay_save = lambda *a, **k: None
    coord._home_frame_store = None
    coord.async_update_listeners = lambda: None

    class _Hass:
        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

    coord.hass = _Hass()
    await coord._async_run_home_frame_registration("s8", _flat(rotate90=True, top=40, left=90), (1, 1, "g"), {})
    assert coord._robot_frame["s8"] == rf6["s6"]
    assert coord._home_frames[rf8["s8"]].get("stale") is True
    assert coord._home_frames[rf8["s8"]]["robots"] == {}
    assert coord._home_frames[rf6["s6"]].get("stale") is not True


def test_old_store_revision_is_not_current() -> None:
    assert not homeframe.storage_is_current({"frames": {}, "robot_frame": {}})
    assert homeframe.storage_is_current(homeframe.frames_to_storage({}, {}))
