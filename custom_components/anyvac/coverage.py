"""Room completion % (docs/45) — pure functions + a per-run tracker.

No Home Assistant imports: the coordinator feeds it trajectory points and room
geometry, and reads per-room results back. Replaces the docs/29 cell-count /
learned-baseline method, which measured "how much like last time" instead of
"how much of the ordered work is done" (docs/45 §1).

Model, verified against the firmware's own progress on a real 2-pass kitchen
run (docs/45 §3):

* A room is a set of 50 mm floor cells taken from the robot's own raw map
  (`homeframe.decode_grid` room ids). The DENOMINATOR is the part of that floor
  a robot body can physically sweep — the morphological opening of the whole
  floor with the robot's footprint disc, restricted to the room — so narrow
  gaps behind furniture never count against a room. No learning, no history:
  moved furniture changes the map and therefore the denominator immediately.
* The NUMERATOR stamps the robot's footprint (radius `FOOTPRINT_MM`) along the
  densified trajectory. Covered cells are a set of absolute cell keys, so a
  map that grows or shifts mid-run never invalidates what was measured.
* Passes: Roborock repeats the whole room (wall lap + lanes) N times. Pass 1 is
  measured spatially; once the room's coverage saturates (the robot keeps
  driving over already covered floor), the path length of pass 1 is known and
  every further pass is measured as path length relative to it.

Without a decoded grid (grid self-test failed, unit tests) a room falls back to
its bounding box rasterised on the same 50 mm grid — same code path, coarser
denominator.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

CELL_MM = 50
# Robot body radius — both the stamped footprint and the reachability opening.
# 175 mm fits the whole fleet (S6/S7/S8 are all ~350 mm across) and on the real
# kitchen run gives 96 % of the reachable floor after pass 1 (docs/45 §3.2).
FOOTPRINT_MM = 175
# Coverage of the reachable floor that already counts as a complete pass.
FULL_FRACTION = 0.95
# Pass-1 saturation: coverage at least this high, and the last
# `SAT_WINDOW_MM` of path added less than `SAT_MAX_GAIN` of the reachable floor.
SAT_MIN_FRACTION = 0.85
SAT_WINDOW_MM = 3000.0
SAT_MAX_GAIN = 0.01
# Densification step between two firmware trajectory points (they are
# ~120 mm apart on average, up to ~330 mm).
STEP_MM = 25.0
# Room activation dwell (path length the robot must drive in a room before it
# counts as the room being cleaned, not driven through): short for the room
# the configured sequence says is next, long for any other target room.
DWELL_NEXT_MM = 1500.0
DWELL_OTHER_MM = 6000.0


def key(ix: int, iy: int) -> int:
    """Absolute cell key — independent of any one grid's origin."""
    return (int(ix) << 20) | (int(iy) & 0xFFFFF)


def _disk(radius_mm: float) -> np.ndarray:
    r = int(math.ceil(radius_mm / CELL_MM))
    out = [
        (dy, dx)
        for dy in range(-r, r + 1)
        for dx in range(-r, r + 1)
        if (dx * CELL_MM) ** 2 + (dy * CELL_MM) ** 2 <= radius_mm * radius_mm
    ]
    return np.array(out, dtype=np.int64)


_DISK = _disk(FOOTPRINT_MM)


def _shift_any(mask: np.ndarray, offsets: np.ndarray, want: bool) -> np.ndarray:
    """Binary erosion (want=True: all neighbours set) / dilation (want=False:
    any neighbour set) of `mask` by the offset list — plain numpy, no scipy."""
    h, w = mask.shape
    r = int(np.abs(offsets).max()) if len(offsets) else 0
    pad = np.pad(mask, r, constant_values=False)
    out = np.ones_like(mask) if want else np.zeros_like(mask)
    for dy, dx in offsets:
        win = pad[r + dy : r + dy + h, r + dx : r + dx + w]
        out = (out & win) if want else (out | win)
    return out


def reachable(floor: np.ndarray) -> np.ndarray:
    """Morphological opening of the floor mask with the robot disc: every cell a
    robot body placed anywhere on the floor can sweep."""
    return _shift_any(_shift_any(floor, _DISK, True), _DISK, False)


@dataclass
class RoomGeom:
    """One room's denominator: absolute keys of its reachable floor cells."""

    name: str
    segment_id: Any
    reach: frozenset[int]


@dataclass
class Geometry:
    """Point -> room lookup + per-room reachable floor for one robot's map."""

    rooms: dict[str, RoomGeom]
    # grid lookup (None = bbox fallback)
    room_id: np.ndarray | None = None
    left: int = 0
    top: int = 0
    seg_names: dict[int, str] = field(default_factory=dict)
    # bbox fallback: (name, x0, y0, x1, y1) smallest-first
    boxes: list[tuple[str, float, float, float, float]] = field(default_factory=list)

    def room_at(self, x: float, y: float) -> str | None:
        if self.room_id is not None:
            ix = int(x // CELL_MM) - self.left
            iy = int(y // CELL_MM) - self.top
            h, w = self.room_id.shape
            if 0 <= ix < w and 0 <= iy < h:
                return self.seg_names.get(int(self.room_id[iy, ix]))
            return None
        for nm, lx, ly, hx, hy in self.boxes:
            if lx <= x <= hx and ly <= y <= hy:
                return nm
        return None


def geometry_from_grid(grid: Any, rooms: list[dict[str, Any]]) -> Geometry:
    """Build from a `homeframe.Grid` + the device's room list (segment_id/name)."""
    seg_names = {
        int(r["segment_id"]): r["name"]
        for r in rooms
        if r.get("name") and r.get("segment_id") is not None
    }
    reach_all = reachable(grid.floor)
    out: dict[str, RoomGeom] = {}
    for seg, nm in seg_names.items():
        ys, xs = np.nonzero(reach_all & (grid.room_id == seg))
        keys = frozenset(key(grid.left + x, grid.top + y) for x, y in zip(xs.tolist(), ys.tolist()))
        if keys:
            out[nm] = RoomGeom(nm, seg, keys)
    return Geometry(out, grid.room_id, grid.left, grid.top, seg_names)


def geometry_from_bboxes(rooms: list[dict[str, Any]]) -> Geometry:
    """Fallback without a decoded grid: rooms are their bounding boxes."""
    out: dict[str, RoomGeom] = {}
    boxes: list[tuple[str, float, float, float, float]] = []
    for r in rooms:
        nm = r.get("name")
        x0, y0, x1, y1 = r.get("x0"), r.get("y0"), r.get("x1"), r.get("y1")
        if not nm or None in (x0, y0, x1, y1):
            continue
        lx, ly, hx, hy = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
        boxes.append((nm, lx, ly, hx, hy))
        out[nm] = RoomGeom(
            nm,
            r.get("segment_id"),
            frozenset(
                key(ix, iy)
                for ix in range(int(lx // CELL_MM), int(hx // CELL_MM) + 1)
                for iy in range(int(ly // CELL_MM), int(hy // CELL_MM) + 1)
            ),
        )
    boxes.sort(key=lambda b: (b[3] - b[1]) * (b[4] - b[2]))
    return Geometry(out, boxes=boxes)


def densify(prev: tuple[float, float] | None, pts: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """Points every <= STEP_MM along the polyline prev->pts, as (x, y, seg_len)
    where seg_len is the path length the point stands for."""
    out: list[tuple[float, float, float]] = []
    last = prev
    for x, y in pts:
        if last is None:
            out.append((x, y, 0.0))
        else:
            dx, dy = x - last[0], y - last[1]
            d = math.hypot(dx, dy)
            n = max(1, int(d // STEP_MM))
            for j in range(1, n + 1):
                out.append((last[0] + dx * j / n, last[1] + dy * j / n, d / n))
        last = (x, y)
    return out


def stamp(x: float, y: float) -> Iterable[int]:
    ix, iy = int(x // CELL_MM), int(y // CELL_MM)
    return (key(ix + dx, iy + dy) for dy, dx in _DISK)


@dataclass
class KindRun:
    """One room x one clean kind (dry/wet) within a run."""

    cells: set[int] = field(default_factory=set)
    length: float = 0.0
    l1: float | None = None
    hits: int = 0  # covered reachable cells (running count)
    window: deque = field(default_factory=deque)  # (length, hits)

    def add(self, x: float, y: float, seg_len: float, reach: frozenset[int], passes: int) -> None:
        for k in stamp(x, y):
            if k not in self.cells:
                self.cells.add(k)
                if k in reach:
                    self.hits += 1
        self.length += seg_len
        if passes < 2 or self.l1 is not None or not reach:
            return
        self.window.append((self.length, self.hits))
        while self.window and self.length - self.window[0][0] > SAT_WINDOW_MM:
            self.window.popleft()
        n = len(reach)
        if (
            self.hits >= SAT_MIN_FRACTION * n
            and self.length - self.window[0][0] >= SAT_WINDOW_MM * 0.9
            and self.hits - self.window[0][1] < SAT_MAX_GAIN * n
        ):
            self.l1 = self.window[0][0]

    def floor_fraction(self, reach: frozenset[int]) -> float:
        return self.hits / len(reach) if reach else 0.0

    def progress(self, reach: frozenset[int], passes: int) -> float:
        p1 = min(1.0, self.floor_fraction(reach) / FULL_FRACTION)
        n = max(1, passes)
        if n == 1:
            return p1
        if self.l1 is None or self.l1 <= 0:
            return p1 / n
        extra = min(n - 1, max(0.0, (self.length - self.l1) / self.l1))
        return min(1.0, (1.0 + extra) / n)

    def current_pass(self, passes: int) -> int:
        n = max(1, passes)
        if self.l1 is None or self.l1 <= 0:
            return 1
        return min(n, 2 + int(max(0.0, self.length - self.l1) // self.l1))


# Firmware trajectory points are <= ~330 mm apart while driving; a longer jump
# is a gap (mop lifted, map re-anchored) and must not be bridged by stamping.
MAX_JOIN_MM = 600.0
KINDS = ("dry", "wet")


class RunTracker:
    """Everything one RUN (docs/36) measures per room: which rooms were really
    cleaned (not driven through), their covered cells and path length per kind.

    Room activation (docs/45 §2.2): a point only counts for a room the run is
    actually cleaning. Rooms outside the run's targets and rooms already
    finished are ignored outright. Entering any other target room starts a
    dwell: after `DWELL_NEXT_MM` of path (the room the configured sequence says
    is next) or `DWELL_OTHER_MM` (any other target — sequence unknown or
    wrong) the room becomes the active one, its buffered points are credited,
    and the previously active room is finished. A pass through a room that is
    shorter than the dwell never becomes coverage.
    """

    def __init__(self) -> None:
        self.rooms: dict[str, dict[str, KindRun]] = {}
        self.active: str | None = None
        self.done: list[str] = []
        self.activated: list[str] = []
        self.passes: int = 1
        self.ignored: dict[str, float] = {}
        self._cand: str | None = None
        self._cand_len = 0.0
        self._cand_buf: list[tuple[float, float, float]] = []
        self._last: dict[str, tuple[float, float] | None] = {"dry": None, "wet": None}
        self._geo_id: int | None = None

    # -- helpers ---------------------------------------------------------------

    def break_path(self, layer: str) -> None:
        """The firmware restarted its trajectory buffer — never join across."""
        self._last[layer] = None

    def _kind(self, room: str, kind: str) -> KindRun:
        return self.rooms.setdefault(room, {}).setdefault(kind, KindRun())

    def _sync_geometry(self, geo: Geometry) -> None:
        if self._geo_id == id(geo):
            return
        self._geo_id = id(geo)
        for room, kinds in self.rooms.items():
            reach = geo.rooms[room].reach if room in geo.rooms else frozenset()
            for kr in kinds.values():
                kr.hits = len(kr.cells & reach)
                kr.window.clear()

    def _points(self, layer: str, pts: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
        out: list[tuple[float, float, float]] = []
        last = self._last[layer]
        for x, y in pts:
            if last is not None and math.hypot(x - last[0], y - last[1]) > MAX_JOIN_MM:
                last = None
            out.extend(densify(last, [(x, y)]))
            last = (x, y)
        self._last[layer] = last
        return out

    def _credit(self, geo: Geometry, room: str, kind: str, x: float, y: float, s: float) -> None:
        rg = geo.rooms.get(room)
        if rg is not None:
            self._kind(room, kind).add(x, y, s, rg.reach, self.passes)

    def _finish_active(self) -> None:
        if self.active is not None and self.active not in self.done:
            self.done.append(self.active)
        self.active = None

    # -- feeding -----------------------------------------------------------------

    def feed(
        self,
        geo: Geometry,
        path_pts: list[tuple[float, float]],
        mop_pts: list[tuple[float, float]],
        *,
        targets: set[str] | None,
        next_room: str | None,
        vacuuming: bool,
        restrict: bool = False,
    ) -> dict[str, float]:
        """Attribute one poll's NEW trajectory points. `path_pts` is the full
        trajectory (decides which room is being cleaned; dry coverage only while
        `vacuuming`), `mop_pts` the mop-down subset (wet coverage). `restrict`
        = the poll that ends active cleaning (return / mop-wash trip starts):
        its points still belong to the active room — the firmware delivers the
        last seconds of cleaning together with the new state — but nothing new
        may activate. Returns the path length credited per room (time weights)."""
        self._sync_geometry(geo)
        credited: dict[str, float] = {}
        touched: set[str] = {self.active} if self.active else set()
        for x, y, s in self._points("dry", path_pts):
            room = geo.room_at(x, y)
            if room is None:
                continue
            if room == self.active:
                self._cand, self._cand_len, self._cand_buf = None, 0.0, []
                if vacuuming:
                    self._credit(geo, room, "dry", x, y, s)
                credited[room] = credited.get(room, 0.0) + s
                continue
            if restrict or (targets is not None and room not in targets) or room in self.done:
                self.ignored[room] = self.ignored.get(room, 0.0) + s
                self._cand, self._cand_len, self._cand_buf = None, 0.0, []
                continue
            if self._cand != room:
                self._cand, self._cand_len, self._cand_buf = room, 0.0, []
            self._cand_len += s
            self._cand_buf.append((x, y, s))
            need = DWELL_NEXT_MM if room == next_room else DWELL_OTHER_MM
            if self._cand_len < need:
                continue
            self._finish_active()
            self.active = room
            if room not in self.activated:
                self.activated.append(room)
            touched.add(room)
            for bx, by, bs in self._cand_buf:
                if vacuuming:
                    self._credit(geo, room, "dry", bx, by, bs)
                credited[room] = credited.get(room, 0.0) + bs
            self._cand, self._cand_len, self._cand_buf = None, 0.0, []
        for x, y, s in self._points("wet", mop_pts):
            room = geo.room_at(x, y)
            if room is not None and room in touched and room in self.activated:
                self._credit(geo, room, "wet", x, y, s)
        return credited

    # -- results -----------------------------------------------------------------

    def kind_result(self, geo: Geometry | None, room: str, kind: str) -> dict[str, Any] | None:
        kr = (self.rooms.get(room) or {}).get(kind)
        if kr is None or not kr.cells or geo is None or room not in geo.rooms:
            return None
        reach = geo.rooms[room].reach
        return {
            "pct": 100 if room in self.done else round(100 * kr.progress(reach, self.passes)),
            "floor": round(100 * kr.floor_fraction(reach)),
            "pass": self.passes if room in self.done else kr.current_pass(self.passes),
        }

    def floor_fraction(self, geo: Geometry | None, room: str, kind: str) -> float:
        kr = (self.rooms.get(room) or {}).get(kind)
        if kr is None or geo is None or room not in geo.rooms:
            return 0.0
        return kr.floor_fraction(geo.rooms[room].reach)
