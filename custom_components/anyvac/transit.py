"""Travel time between rooms and the dock (docs/53 §3).

Default model: the shortest path along the floor of the robot's own map
(BFS on its 50 mm room grid, 4-neighbour, plain numpy) divided by a travel
speed. Each room is represented by the floor cell nearest to its centroid;
the dock by the cell of the charger. The firmware picks where it enters a
room, so this is an estimate — measured transits (coordinator) replace it.

Pure functions: no Home Assistant here, so the planner and the runner can
use them in tests as they are.
"""

from __future__ import annotations

from typing import Any

import numpy as np

CELL_MM = 50
# Driving between rooms (not cleaning), docs/53 §3: ~0.3 m/s.
TRAVEL_MM_S = 300.0
DOCK = "dock"


def _anchor(mask: np.ndarray) -> tuple[int, int] | None:
    """Cell of `mask` nearest to the mask's centroid (so an L-shaped room's
    anchor still lies inside the room)."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    cy, cx = ys.mean(), xs.mean()
    i = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
    return int(ys[i]), int(xs[i])


def _bfs(floor: np.ndarray, start: tuple[int, int], targets: list[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Steps from `start` to each target along `floor` (4-neighbour), by
    repeated dilation of the frontier. Unreached targets are left out."""
    h, w = floor.shape
    seen = np.zeros_like(floor)
    frontier = np.zeros_like(floor)
    sy, sx = start
    frontier[sy, sx] = True
    seen[sy, sx] = True
    left = {t for t in targets}
    out: dict[tuple[int, int], int] = {}
    steps = 0
    limit = h * w
    while left and frontier.any() and steps <= limit:
        for t in list(left):
            if frontier[t]:
                out[t] = steps
                left.discard(t)
        nxt = np.zeros_like(floor)
        nxt[1:, :] |= frontier[:-1, :]
        nxt[:-1, :] |= frontier[1:, :]
        nxt[:, 1:] |= frontier[:, :-1]
        nxt[:, :-1] |= frontier[:, 1:]
        nxt &= floor & ~seen
        seen |= nxt
        frontier = nxt
        steps += 1
    return out


def floor_minutes(
    room_id: np.ndarray,
    left: int,
    top: int,
    seg_names: dict[int, str],
    charger_mm: dict[str, Any] | None,
    speed_mm_s: float = TRAVEL_MM_S,
) -> dict[str, dict[str, float]]:
    """{from: {to: minutes}} between every room and the dock (`DOCK`), along
    the floor. A pair the floor does not connect gets the straight line × 1.4."""
    floor = room_id > 0
    anchors: dict[str, tuple[int, int]] = {}
    for seg, name in seg_names.items():
        a = _anchor(room_id == seg)
        if a is not None:
            anchors[name] = a
    if charger_mm and charger_mm.get("x") is not None and charger_mm.get("y") is not None:
        cy = int(float(charger_mm["y"]) // CELL_MM) - top
        cx = int(float(charger_mm["x"]) // CELL_MM) - left
        h, w = floor.shape
        if 0 <= cy < h and 0 <= cx < w:
            if not floor[cy, cx]:
                # The dock stands at the wall: take the nearest floor cell.
                ys, xs = np.nonzero(floor)
                if len(xs):
                    i = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
                    cy, cx = int(ys[i]), int(xs[i])
            anchors[DOCK] = (cy, cx)
    per_min = CELL_MM / speed_mm_s / 60.0
    out: dict[str, dict[str, float]] = {}
    names = list(anchors)
    for a in names:
        steps = _bfs(floor, anchors[a], [anchors[b] for b in names if b != a])
        row: dict[str, float] = {}
        for b in names:
            if b == a:
                continue
            n = steps.get(anchors[b])
            if n is None:
                (ay, ax), (by, bx) = anchors[a], anchors[b]
                n = 1.4 * ((ay - by) ** 2 + (ax - bx) ** 2) ** 0.5
            row[b] = round(n * per_min, 2)
        out[a] = row
    return out


# The start from the dock is slower than the floor model knows: the command
# reaches the robot, it backs out and finds itself (field 2026-11-27: S6
# 31 s from the command to the first cleaning state, ~1.1 min dock → Bathroom
# for S6 and S8 against ~0.3 min of floor). Measured per robot as what its
# learned starts from the dock take over the floor model, at most this.
DOCK_START_MAX = 2.0


# Measured transits kept per pair; the value used is their low median.
SAMPLES_KEPT = 5


def low_median(values: list[float]) -> float:
    """The lower of the two middle values: a transit can only come out longer
    than the drive (another robot in the way, an obstacle, a late snapshot),
    never shorter — a single slow one must not set the time (field
    2026-11-27: S8 met S6 in the kitchen on its way, 1.4 min instead of ~0.4)."""
    vals = sorted(values)
    return vals[(len(vals) - 1) // 2]


def dock_start(default: dict[str, dict[str, float]], learned: dict[str, dict[str, float]]) -> float:
    """Minutes a start from the dock takes over the floor model: the mean of
    (measured − floor) over the rooms this robot was measured going to from
    the dock; 0 before the first one."""
    row_d = (default or {}).get(DOCK) or {}
    row_l = (learned or {}).get(DOCK) or {}
    diffs = [
        float(v) - float(row_d[b])
        for b, v in row_l.items()
        if isinstance(v, (int, float)) and isinstance(row_d.get(b), (int, float))
    ]
    if not diffs:
        return 0.0
    return round(min(DOCK_START_MAX, max(0.0, low_median(diffs))), 2)


def blend(default: dict[str, dict[str, float]], learned: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """The floor model with the measured transits on top (docs/53 §3:
    measured wins). A start from the dock to a room not measured yet gets the
    robot's measured start overhead (`dock_start`) on top of the floor."""
    start = dock_start(default, learned)
    out = {a: dict(row) for a, row in default.items()}
    if start and DOCK in out:
        out[DOCK] = {b: round(v + start, 2) for b, v in out[DOCK].items()}
    for a, row in (learned or {}).items():
        for b, v in (row or {}).items():
            if isinstance(v, (int, float)):
                out.setdefault(a, {})[b] = float(v)
    return out
