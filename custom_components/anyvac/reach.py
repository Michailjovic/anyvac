"""Reach mask (docs/51 §2) — where a robot's body has actually been, per room.

No Home Assistant imports: the coordinator hands in the covered cells a finished
run measured (`coverage.KindRun.cells`, docs/45) and gets back a compact,
storable record; the renderer turns the stored records into an image on the
robot's own map canvas.

Rules (docs/51 §2.2):

* One record per (robot, room), always REPLACED by the robot's next complete
  clean of that room — moved furniture is picked up by one clean of that room,
  and the store never grows with history.
* A room is recorded only when the run really cleaned it (`RunTracker.activated`)
  and swept at least `REACH_MIN_FLOOR` of its reachable floor — a cancelled or
  half-finished clean never overwrites a good mask with a partial one.
* The cells are the robot's swept footprint (disc of `FOOTPRINT_MM` along the
  trajectory), not the trajectory itself: the mask's edge is where the body
  reached, and the holes inside it are where it never fitted — under furniture.

Coordinates are the robot's own mm, on the same 50 mm absolute grid
`coverage.key` uses. A record therefore only means something for the map it was
measured on; it carries the room's `segment_id` and the `map_index` so a
re-mapped robot (new segment ids after the map was deleted and redrawn in the
Roborock app) shows the old record as stale instead of drawing it in the wrong
place (§2.4).
"""

from __future__ import annotations

import base64
import io
import zlib
from typing import Any, Iterable

import numpy as np

from .coverage import CELL_MM

# Share of a room's reachable floor a clean must sweep to replace its mask.
REACH_MIN_FLOOR = 0.80
# Fill colour of the published raster (RGBA). Distinct from the guide layers'
# magenta / lime / cyan (docs/37) so the two can be stacked in an editor.
REACH_COLOR = (255, 140, 0, 255)


def decode_key(k: int) -> tuple[int, int]:
    """Inverse of `coverage.key`."""
    return int(k) >> 20, int(k) & 0xFFFFF


def encode_cells(cells: Iterable[int]) -> dict[str, Any] | None:
    """Pack absolute cell keys into a bounding-box bitmap: origin, size and
    `base64(zlib(packbits))`. None for an empty set."""
    pts = [decode_key(k) for k in cells]
    if not pts:
        return None
    arr = np.array(pts, dtype=np.int64)
    x0, y0 = int(arr[:, 0].min()), int(arr[:, 1].min())
    w = int(arr[:, 0].max()) - x0 + 1
    h = int(arr[:, 1].max()) - y0 + 1
    mask = np.zeros((h, w), dtype=bool)
    mask[arr[:, 1] - y0, arr[:, 0] - x0] = True
    bits = base64.b64encode(zlib.compress(np.packbits(mask.ravel()).tobytes(), 9)).decode("ascii")
    return {"x0": x0, "y0": y0, "w": w, "h": h, "bits": bits}


def decode_mask(rec: dict[str, Any]) -> tuple[int, int, np.ndarray] | None:
    """`(x0, y0, mask)` of one stored record, or None when it is malformed."""
    try:
        x0, y0, w, h = int(rec["x0"]), int(rec["y0"]), int(rec["w"]), int(rec["h"])
        if w <= 0 or h <= 0:
            return None
        raw = np.frombuffer(zlib.decompress(base64.b64decode(rec["bits"])), dtype=np.uint8)
        flat = np.unpackbits(raw)[: w * h]
        if flat.size != w * h:
            return None
        return x0, y0, flat.reshape(h, w).astype(bool)
    except (KeyError, TypeError, ValueError, zlib.error):
        return None


def make_record(
    cells: Iterable[int],
    reach: Iterable[int],
    *,
    segment_id: Any,
    kinds: list[str],
    map_index: Any,
    at: str,
) -> dict[str, Any] | None:
    """A room's new record, or None when the clean did not sweep enough of the
    room's reachable floor to replace whatever is stored (`REACH_MIN_FLOOR`)."""
    cells = set(cells)
    reach = set(reach)
    if not cells or not reach:
        return None
    floor = len(cells & reach) / len(reach)
    if floor < REACH_MIN_FLOOR:
        return None
    packed = encode_cells(cells)
    if packed is None:
        return None
    return {
        **packed,
        "segment_id": segment_id,
        "map_index": map_index,
        "kinds": sorted(kinds),
        "floor_pct": round(100 * floor),
        "at": at,
    }


def is_stale(rec: dict[str, Any], segment_id_now: Any, map_index_now: Any) -> bool:
    """A record measured on a different map than the robot has now: the room is
    gone, its segment id changed (map deleted and redrawn), or the map slot
    changed. Unknown values on either side never make a record stale."""
    if segment_id_now is None:
        return True
    seg = rec.get("segment_id")
    if seg is not None and str(seg) != str(segment_id_now):
        return True
    mi = rec.get("map_index")
    return mi is not None and map_index_now is not None and mi != map_index_now


def union_mask(records: Iterable[dict[str, Any]]) -> tuple[int, int, np.ndarray] | None:
    """One bitmap covering every record — the robot's whole reach."""
    parts = [m for r in records if (m := decode_mask(r)) is not None]
    if not parts:
        return None
    x0 = min(p[0] for p in parts)
    y0 = min(p[1] for p in parts)
    x1 = max(p[0] + p[2].shape[1] for p in parts)
    y1 = max(p[1] + p[2].shape[0] for p in parts)
    out = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    for px, py, m in parts:
        out[py - y0 : py - y0 + m.shape[0], px - x0 : px - x0 + m.shape[1]] |= m
    return x0, y0, out


def render_rgba(
    x0: int,
    y0: int,
    mask: np.ndarray,
    aff: tuple[float, float, float, float, float, float],
    width: int,
    height: int,
    color: tuple[int, int, int, int] = REACH_COLOR,
) -> np.ndarray | None:
    """Rasterise the mask onto the robot's own rendered map canvas (`width` x
    `height` px, the space `aff` maps mm into — the same affine every `*_px`
    attribute uses). Every output pixel is pulled back to mm through the inverse
    affine and looked up in the mask, so rotation, flips and scale need no
    special cases. Returns an RGBA array, or None when nothing lands on it."""
    if width <= 0 or height <= 0:
        return None
    a, b, c, d, e, f = aff
    det = a * e - b * d
    if abs(det) < 1e-12:
        return None
    h, w = mask.shape
    # Only the window the mask can land on is evaluated — the robot's canvas is
    # much larger than the home (Roborock pads it), and a full-canvas float
    # grid is tens of MB on a big map.
    xs = np.array([x0, x0 + w, x0, x0 + w], dtype=np.float64) * CELL_MM
    ys = np.array([y0, y0, y0 + h, y0 + h], dtype=np.float64) * CELL_MM
    us, vs = a * xs + b * ys + c, d * xs + e * ys + f
    u0 = max(0, int(np.floor(us.min())) - 1)
    u1 = min(width, int(np.ceil(us.max())) + 1)
    v0 = max(0, int(np.floor(vs.min())) - 1)
    v1 = min(height, int(np.ceil(vs.max())) + 1)
    if u0 >= u1 or v0 >= v1:
        return None
    u = np.arange(u0, u1, dtype=np.float32) + np.float32(0.5 - c)
    v = np.arange(v0, v1, dtype=np.float32) + np.float32(0.5 - f)
    uu, vv = np.meshgrid(u, v)
    ix = np.floor((e * uu - b * vv) / (det * CELL_MM)).astype(np.int32) - x0
    iy = np.floor((-d * uu + a * vv) / (det * CELL_MM)).astype(np.int32) - y0
    inside = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
    win = np.zeros(uu.shape, dtype=bool)
    win[inside] = mask[iy[inside], ix[inside]]
    if not win.any():
        return None
    out = np.zeros((height, width, 4), dtype=np.uint8)
    out[v0:v1, u0:u1][win] = color
    return out


def png_bytes(rgba: np.ndarray, crop: tuple[int, int, int, int] | None = None) -> bytes:
    """PNG of an RGBA array, optionally cut to `(x0, y0, x1, y1)`."""
    from PIL import Image

    img = Image.fromarray(rgba, "RGBA")
    if crop is not None:
        img = img.crop(crop)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
