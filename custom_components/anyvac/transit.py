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


def blend(default: dict[str, dict[str, float]], learned: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """The floor model with the measured transits on top (docs/53 §3:
    measured wins)."""
    out = {a: dict(row) for a, row in default.items()}
    for a, row in (learned or {}).items():
        for b, v in (row or {}).items():
            if isinstance(v, (int, float)):
                out.setdefault(a, {})[b] = float(v)
    return out
