"""Home frame — automatic multi-vacuum map registration (docs/40 Fáze 1).

Pure functions, numpy, NO Home Assistant imports at module scope (the
coordinator calls these from `hass.async_add_executor_job` — see
`coordinator.py`'s home-frame integration). This module is the Fáze 0
`anyvac/tools/homeframe_probe.py` spike, promoted: `decode_grid`, `register`/
`register_with_fallback` and `kabsch_from_names` are carried over UNCHANGED
(the probe now imports them from here instead of defining its own copies —
docs/14 rule 1, one implementation). New in Fáze 1: `grid_self_test`,
mm<->home-px/frame transforms, `outline_from_mask`, `room_identity`.

Coordinate systems, to keep straight while reading this file:

- A robot's OWN raw grid (`Grid`, from `decode_grid`): one cell = `CELL_MM`
  (50mm); `grid.top`/`grid.left` place the grid's cell (0,0) in the shared
  Roborock mm space via `cell_to_mm`.
- A HOME FRAME: a shared raster with its OWN fixed `origin_mm` (docs/40
  §4.2 — set once, at frame creation, from the founding robot's grid origin
  minus `FRAME_MARGIN_MM` in both axes so the frame can grow toward -x/-y a
  little before an origin shift is needed). `floor_mask`/`wall_mask` are
  stored at the frame's own cell resolution (`FRAME_CELL_MM` == `CELL_MM` —
  every robot shares one physical mm scale, docs/40 §3, so there is no
  reason for the frame to use a different cell size). Published `*_home_px`
  attributes use `HOME_PX_SCALE` (4, same density as today's `*_px` — 12.5
  mm/px, docs/40 §2) — one frame CELL is `HOME_PX_SCALE` home PIXELS.
- A robot's registration is stored as a plain 2D rigid transform
  (`rot_deg`, `tx_mm`, `ty_mm` — no scale, docs/40 §3: all robots share one
  physical mm scale) mapping the robot's OWN mm coordinates directly onto
  the frame's mm coordinates: ``frame_mm = R(rot_deg) @ robot_mm + T_mm``.
  For the founding ("reference") robot this is always the identity
  (rot_deg=0, tx_mm=0, ty_mm=0) — the frame's shared mm system IS that
  robot's own mm system, just extended with margin for storage; `origin_mm`
  only affects which CELL a given mm position falls in, never the mm
  values themselves. `affine_from_registration` below is what turns a raw
  `register()` result (a mask correlation in CELL units, pivoted about the
  candidate grid's own array centre) into this clean, self-contained
  (rot_deg, tx_mm, ty_mm) triple — do the pivot arithmetic ONCE there, at
  registration time, never per-point at read time. The founding robot never
  goes through `register()`/`affine_from_registration` at all — it defines
  the frame, so the coordinator assigns it (0, 0, 0) directly; feeding a
  hand-rolled "zero" `Registration` into `affine_from_registration` does
  NOT produce (0, 0) in general (see that function's docstring).
"""

from __future__ import annotations

import base64
import math
import struct
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

# 1 raw grid cell = 50 mm (docs/40 §2) — fixed by the Roborock map format.
CELL_MM = 50
# The frame's own raster uses the SAME cell size — all robots share one
# physical mm scale (docs/40 §3), so there is never a reason for the frame
# to resample to a different one.
FRAME_CELL_MM = CELL_MM
# Published `*_home_px` density: 1 home px = 1/HOME_PX_SCALE frame cells, i.e.
# FRAME_CELL_MM / HOME_PX_SCALE mm/px — 50mm / 4 = 12.5 mm/px, matching
# today's rendered `*_px` density exactly (those scale a rendered image whose
# own pixels are 1 raw cell each, at `image_dims.scale`, typically 4 —
# same ratio, different derivation). Kept as its own named constant so a
# future session isn't left guessing which "4" this is.
HOME_PX_SCALE = 4

# vacuum_map_parser_roborock.map_data_parser.RoborockBlockType.IMAGE.value —
# hardcoded (not imported) so `decode_grid` has zero import-time dependency
# on that library; `grid_self_test` is what cross-checks against it.
_BLOCK_TYPE_IMAGE = 2

# docs/40 §4.2 default gate — overridable by the caller once real-world
# experience (Fáze 0's field report) suggests different numbers.
DEFAULT_GATE_COVERED = 0.6
DEFAULT_GATE_IOU = 0.4

# How far (mm) beyond the founding robot's own grid origin a frame's array
# starts, in both +/-x and +/-y — headroom so a small amount of frame growth
# toward -x/-y doesn't immediately force an origin shift (docs/40 §4.2).
FRAME_MARGIN_MM = 5000


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


@dataclass
class Grid:
    """One robot's raw map, decoded straight from bytes (docs/40 §2).

    `room_id`/`floor`/`wall` are HxW numpy arrays in the map's OWN raw pixel
    grid (row-major, `idx = x + width*y`, NOT y-flipped). `top`/`left` place
    the grid's cell (0,0) in the shared Roborock mm space — see `cell_to_mm`.
    """

    room_id: np.ndarray  # uint8 HxW, 0 = not a room cell, else segment id
    floor: np.ndarray  # bool HxW — room_id > 0 (registration only needs this)
    wall: np.ndarray  # bool HxW — bonus, for outline/snap use
    top: int
    left: int
    width: int
    height: int
    map_index: int | None
    map_sequence: int | None


def decode_grid(raw: bytes) -> Grid:
    """Decode a raw Roborock map blob's IMAGE block into numpy masks.

    Every offset here is a literal transcription of docs/40 §2 (verified
    against `vacuum_map_parser_roborock` 0.1.5 / `python-roborock` 7.5.0
    source AND against three real robots' raw dumps in Fáze 0 — self-test
    passed on all three). Do NOT "clean up" the arithmetic without
    re-checking against that source; a single off-by-one here silently
    corrupts every downstream mm coordinate.
    """
    if len(raw) < 0x14:
        raise ValueError("homeframe.decode_grid: blob too short for a map header")

    map_header_length = _u16(raw, 0x02)
    map_index = _i32(raw, 0x0C)
    map_sequence = _i32(raw, 0x10)

    pos = map_header_length
    image_top = image_left = image_width = image_height = None
    image_data: bytes | None = None

    while pos + 8 <= len(raw):
        block_type = _u16(raw, pos + 0x00)
        block_header_length = _u16(raw, pos + 0x02)
        block_data_length = _i32(raw, pos + 0x04)
        if block_header_length <= 0 or block_data_length < 0:
            break  # corrupt or unrecognised layout — stop rather than read garbage
        header_end = pos + block_header_length
        if block_type == _BLOCK_TYPE_IMAGE:
            if header_end - 16 < 0 or header_end + block_data_length > len(raw):
                raise ValueError(
                    "homeframe.decode_grid: IMAGE block header/data out of bounds"
                )
            image_top = _i32(raw, header_end - 16)
            image_left = _i32(raw, header_end - 12)
            image_height = _i32(raw, header_end - 8)
            image_width = _i32(raw, header_end - 4)
            image_data = raw[header_end : header_end + block_data_length]
            break  # nothing else in the blob is needed for registration
        pos = header_end + block_data_length

    if image_data is None or not image_width or not image_height:
        raise ValueError("homeframe.decode_grid: no IMAGE block found in raw map")

    expected = image_width * image_height
    if len(image_data) < expected:
        raise ValueError(
            f"homeframe.decode_grid: IMAGE block too short "
            f"({len(image_data)} bytes, expected {expected} for "
            f"{image_width}x{image_height})"
        )

    grid = np.frombuffer(image_data[:expected], dtype=np.uint8).reshape(
        image_height, image_width
    )
    special = np.isin(grid, [0x00, 0x01, 0xFF, 0x07])
    obstacle = grid & 7
    is_room = (~special) & (obstacle == 7)
    room_id = np.where(is_room, grid >> 3, 0).astype(np.uint8)
    wall = (grid == 0x01) | ((~special) & np.isin(obstacle, [0, 1]))

    return Grid(
        room_id=room_id,
        floor=room_id > 0,
        wall=wall,
        top=image_top,
        left=image_left,
        width=image_width,
        height=image_height,
        map_index=map_index,
        map_sequence=map_sequence,
    )


def cell_to_mm(grid: Grid, ix: int, iy: int) -> tuple[int, int]:
    """mm position of raw grid cell (ix, iy) — docs/40 §2's `(left+ix)*50,
    (top+iy)*50`."""
    return (grid.left + ix) * CELL_MM, (grid.top + iy) * CELL_MM


def grid_self_test(grid: Grid, lib_rooms: dict[int, Any]) -> bool:
    """Compare each segment's bbox computed from `grid.room_id` against the
    SAME segment's `Room.x0..y1` from the real library parser's own output
    (`MapData.rooms`) — must match to the cell (docs/40 §2/§3, verified
    against real-robot data in Fáze 0). `lib_rooms` is
    `{segment_number: Room}` exactly as `vacuum_map_parser_base` produces
    it. False means the decoder must NOT be trusted for this map — the
    coordinator disables home-frame registration for that duid (docs/40
    §4.3) rather than publish geometry built on an unverified decode."""
    for seg_id, room in lib_rooms.items():
        mask = grid.room_id == seg_id
        if not mask.any():
            return False
        ys, xs = np.nonzero(mask)
        gx0, gy0 = cell_to_mm(grid, int(xs.min()), int(ys.min()))
        gx1, gy1 = cell_to_mm(grid, int(xs.max()), int(ys.max()))
        lib_bbox = (
            getattr(room, "x0", None),
            getattr(room, "y0", None),
            getattr(room, "x1", None),
            getattr(room, "y1", None),
        )
        if lib_bbox != (gx0, gy0, gx1, gy1):
            return False
    return True


# ── Registration (docs/40 §3.2-3.3, §4.1-4.2) ─────────────────────────────────


@dataclass
class Registration:
    rot_deg: int
    dy_cells: int
    dx_cells: int
    covered: float  # |B∩A| / |B| — partial-map aware (B never has to cover all of A)
    iou: float
    method: str  # "coarse" / "fine" / "fallback-360"
    seconds: float


def _rotate_arbitrary(mask: np.ndarray, deg: float) -> np.ndarray:
    """Nearest-neighbour rotation of a boolean mask by `deg` degrees about its
    own centre, via inverse coordinate rotation into an enlarged (diagonal +
    margin) square canvas so nothing clips — no scipy/PIL dependency.

    Point-transform equivalent (used by `affine_from_registration` below):
    for a point P given relative to the mask's own centre, this maps it to
    ``R(deg) @ P`` where ``R(deg) = [[cos, -sin], [sin, cos]]`` — i.e. the
    ordinary CCW rotation matrix treating the mask's (col, row) axes as an
    ordinary (x, y) plane. That equivalence was verified empirically in the
    Fáze 0 spike (`_rotate_arbitrary(m, 90)` pixel-matches `np.rot90(m,
    k=-1)` exactly) and is load-bearing for `affine_from_registration` — if
    this formula's sign convention ever changes, that function's algebra
    must be re-derived to match."""
    if deg % 360 == 0:
        return mask.copy()
    h, w = mask.shape
    d = int(math.hypot(h, w)) + 4
    out = np.zeros((d, d), dtype=mask.dtype)
    cy, cx = d / 2.0, d / 2.0
    gy, gx = h / 2.0, w / 2.0
    theta = math.radians(deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    ys, xs = np.mgrid[0:d, 0:d]
    sx = cos_t * (xs - cx) + sin_t * (ys - cy) + gx
    sy = -sin_t * (xs - cx) + cos_t * (ys - cy) + gy
    sxi = np.round(sx).astype(int)
    syi = np.round(sy).astype(int)
    ok = (sxi >= 0) & (sxi < w) & (syi >= 0) & (syi < h)
    out[ok] = mask[syi[ok], sxi[ok]]
    return out


def _fft_best_shift(fa: np.ndarray, fb: np.ndarray) -> tuple[int, int]:
    """Best (dy, dx) integer shift of `fb` onto `fa` by FFT cross-correlation,
    zero-padded to >=2x each mask's own size on both axes so the correlation
    is linear, not circular."""
    ha, wa = fa.shape
    hb, wb = fb.shape
    H = max(ha, hb) * 2
    W = max(wa, wb) * 2
    FA = np.fft.rfft2(fa, s=(H, W))
    FB = np.fft.rfft2(fb, s=(H, W))
    xc = np.fft.irfft2(FA * np.conj(FB), s=(H, W))
    idx = np.unravel_index(np.argmax(xc), xc.shape)
    dy = idx[0] if idx[0] < H // 2 else idx[0] - H
    dx = idx[1] if idx[1] < W // 2 else idx[1] - W
    return int(dy), int(dx)


def _score(fa: np.ndarray, fb: np.ndarray, dy: int, dx: int) -> tuple[float, float]:
    shifted = np.zeros_like(fa)
    ys, xs = np.nonzero(fb)
    ys2, xs2 = ys + dy, xs + dx
    ok = (ys2 >= 0) & (ys2 < fa.shape[0]) & (xs2 >= 0) & (xs2 < fa.shape[1])
    shifted[ys2[ok], xs2[ok]] = 1
    inter = float((shifted * fa).sum())
    union = float(((shifted + fa) > 0).sum())
    b_total = float(fb.sum())
    covered = inter / b_total if b_total else 0.0
    iou = inter / union if union else 0.0
    return covered, iou


def register(
    mask_b: np.ndarray,
    mask_ref: np.ndarray,
    *,
    coarse: tuple[int, ...] = (0, 90, 180, 270),
    fine: range = range(-6, 7),
) -> Registration:
    """Register `mask_b` (a floor bool mask, candidate robot/grid, ITS OWN
    raw array, cell size CELL_MM) onto `mask_ref` (the frame's own floor
    mask, or a founding robot's own mask). Coarse 4×90° pass (exact
    `np.rot90`, no interpolation loss) + a fine ±6°/1° sweep around the
    coarse winner (nearest-neighbour `_rotate_arbitrary`, composed ON TOP OF
    the winning coarse candidate — never re-derived from `mask_b` with a
    combined angle, see the note in `_rotate_arbitrary`'s docstring), each
    scored by `covered` then `iou` as tiebreak. Validated in Fáze 0 against
    both synthetic maps (exact rotation recovery at 0/90/180/270/93/86°,
    `covered`/`iou` > 0.97 with 2% noise + partial exploration + a canvas
    offset) and three real robots' raw dumps (S6→S7 179°, S8→S7 270°,
    matching docs/38's floorplan-fit expectation of ~180°/~±90°). No scipy
    — see docs/40 §6."""
    t0 = time.perf_counter()
    fa = mask_ref.astype(np.float32)
    fb_base = mask_b.astype(np.float32)

    best: Registration | None = None
    best_fb_coarse: np.ndarray | None = None
    for ang0 in coarse:
        if ang0 % 90 == 0:
            fb_coarse = np.rot90(fb_base, k=(-(ang0 // 90)) % 4)
        else:
            fb_coarse = _rotate_arbitrary(fb_base, ang0)
        dy, dx = _fft_best_shift(fa, fb_coarse)
        covered, iou = _score(fa, fb_coarse, dy, dx)
        cand = Registration(ang0 % 360, dy, dx, covered, iou, "coarse", 0.0)
        if best is None or (cand.covered, cand.iou) > (best.covered, best.iou):
            best = cand
            best_fb_coarse = fb_coarse

    coarse_winner = best.rot_deg if best is not None else 0
    for df in fine:
        if df == 0:
            continue
        fb_fine = _rotate_arbitrary(best_fb_coarse, df)
        dy, dx = _fft_best_shift(fa, fb_fine)
        covered, iou = _score(fa, fb_fine, dy, dx)
        cand = Registration((coarse_winner + df) % 360, dy, dx, covered, iou, "fine", 0.0)
        if best is None or (cand.covered, cand.iou) > (best.covered, best.iou):
            best = cand

    assert best is not None
    best.seconds = time.perf_counter() - t0
    return best


def register_with_fallback(
    mask_b: np.ndarray,
    mask_ref: np.ndarray,
    *,
    threshold_covered: float = DEFAULT_GATE_COVERED,
    threshold_iou: float = DEFAULT_GATE_IOU,
) -> Registration:
    """`register` with the docs/40 §4.2 default gate; if coarse+fine both miss
    it, falls back to a full 360°@2° sweep on a 2x-downsampled grid (cheaper
    per-angle, coarser result — a last resort, not the normal path) before
    giving up."""
    result = register(mask_b, mask_ref)
    if result.covered >= threshold_covered and result.iou >= threshold_iou:
        return result
    t0 = time.perf_counter()
    small_ref = mask_ref[::2, ::2]
    small_b = mask_b[::2, ::2]
    fallback = register(small_b, small_ref, coarse=tuple(range(0, 360, 2)), fine=range(0, 1))
    fallback.dy_cells *= 2
    fallback.dx_cells *= 2
    fallback.method = "fallback-360"
    fallback.seconds = result.seconds + (time.perf_counter() - t0)
    if (fallback.covered, fallback.iou) > (result.covered, result.iou):
        return fallback
    return result


def kabsch_from_names(
    centroids_b: dict[str, tuple[float, float]],
    centroids_ref: dict[str, tuple[float, float]],
) -> tuple[float, tuple[float, float]] | None:
    """Independent second opinion (docs/40 §4.1): given room centroids KEYED
    BY A SHARED IDENTIFIER (room name) for both maps, solves the 2D rigid
    transform (rotation + translation, no scale) via Kabsch. Only ever a
    cross-check next to `register`'s mask-based result, never the primary
    source — needs >=2 shared names. Returns `(rot_deg, (tx_mm, ty_mm))`
    mapping B's mm space onto the reference's, or None when fewer than 2
    names are shared."""
    shared = sorted(set(centroids_b) & set(centroids_ref))
    if len(shared) < 2:
        return None
    b_pts = np.array([centroids_b[k] for k in shared], dtype=np.float64)
    ref_pts = np.array([centroids_ref[k] for k in shared], dtype=np.float64)
    b_mean = b_pts.mean(axis=0)
    ref_mean = ref_pts.mean(axis=0)
    bc = b_pts - b_mean
    refc = ref_pts - ref_mean
    h = bc.T @ refc
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    s = np.diag([1.0, d])
    r = vt.T @ s @ u.T
    rot_deg = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    t = ref_mean - r @ b_mean
    return rot_deg, (float(t[0]), float(t[1]))


# ── mm <-> frame / home-px transforms (docs/40 §4.1) ──────────────────────────


def _rot90_point(r: float, c: float, h: int, w: int, k: int) -> tuple[float, float, int, int]:
    """Exact index-for-index point-transform of `np.rot90(arr, k)` on an
    h(row) x w(col) array — derived (and checked against `np.rot90` itself)
    empirically rather than trusted from memory, since getting this wrong
    would silently corrupt `affine_from_registration` below the same way the
    Fáze 0 `_rotate_arbitrary` sign bug once did. Returns
    `(new_r, new_c, new_h, new_w)` — `k` odd swaps the shape."""
    k = k % 4
    if k == 0:
        return r, c, h, w
    if k == 1:
        return w - 1 - c, r, w, h
    if k == 2:
        return h - 1 - r, w - 1 - c, h, w
    return c, h - 1 - r, w, h  # k == 3


def _rotate_arbitrary_point(r: float, c: float, h: int, w: int, deg: float) -> tuple[float, float, int]:
    """Continuous point-transform matching `_rotate_arbitrary`'s forward
    mapping exactly (re-derived from its inverse-sampling code, not just its
    docstring paraphrase — see the module's git history for the derivation
    if this ever needs re-checking): rotates (r, c) about the h x w array's
    own centre by `deg` and places it in the enlarged square canvas
    `_rotate_arbitrary` outputs (side `d`, itself centred on that same
    physical centre point). Returns `(new_r, new_c, d)`."""
    d = int(math.hypot(h, w)) + 4
    gy, gx = h / 2.0, w / 2.0
    u, v = c - gx, r - gy
    theta = math.radians(deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    big_u = cos_t * u - sin_t * v
    big_v = sin_t * u + cos_t * v
    return big_v + d / 2.0, big_u + d / 2.0, d


def affine_from_registration(
    reg: Registration, grid_b: Grid, frame_origin_mm: tuple[float, float]
) -> tuple[float, float, float]:
    """Turn a raw `register()` result into the clean, self-contained
    `(rot_deg, tx_mm, ty_mm)` triple this module persists and publishes, so
    `robot_mm_to_frame_mm` itself can be a trivial affine apply.

    IMPORTANT — why this replays register()'s rotation instead of using one
    closed-form trig formula: `register()`'s coarse pass rotates via exact
    `np.rot90` (which, for a 90/270 result, SWAPS the array's height/width —
    a different pivot/canvas than `_rotate_arbitrary`'s own square-canvas
    convention), while its fine pass then rotates the coarse WINNER further
    via `_rotate_arbitrary`. A single formula that assumes one fixed pivot
    (as an earlier version of this function did) silently mis-locates every
    non-square grid registered at a rot_deg near a 90-multiple — caught only
    by cross-checking against an actual floor-mask centroid in Fáze 1's own
    validation, not by algebra alone. So instead: replay, index-for-index,
    the EXACT stage(s) `register()` used for `reg.method` (see below) on the
    single point `grid_b`'s own array origin (ix=0, iy=0) — cheap, and
    correct by construction since it uses the SAME primitives, not a
    re-derived approximation of them. `reg.rot_deg` itself (the ROTATION,
    not the translation) is trusted as-is; Fáze 0 already validated
    `register()` recovers it exactly (0/90/93/180/270° etc.) — only the
    translation needs this replay.

    Per `reg.method`:
    - "coarse": `rot_deg` is always an exact multiple of 90 in the only path
      that reaches this method at the top level (`register_with_fallback`
      relabels any non-multiple coarse hit as "fallback-360") — replay is a
      single `_rot90_point`.
    - "fine": the coarse winner is reconstructible as the nearest multiple
      of 90 to `rot_deg` (register()'s fine sweep is only ever ±6°, so this
      is unambiguous) — replay is `_rot90_point` then `_rotate_arbitrary_point`
      of the remaining `df` degrees, composed, exactly mirroring
      `_rotate_arbitrary(best_fb_coarse, df)` in `register()`.
    - "fallback-360": register()'s downsampled (2x) last-resort path has its
      own, different pivot (rotating a half-resolution array, only rescaling
      the final shift by 2x afterward) that isn't worth replicating exactly
      for a path documented as a rare, coarser-result fallback never yet
      triggered on real data (Fáze 0) — approximated here as a single
      full-resolution `_rotate_arbitrary_point`, which can be off by a
      fraction of one frame cell in this rare case only.

    For the founding/reference robot: NOT called at all — its (0°, 0mm,
    0mm) identity is a direct assignment the coordinator makes at
    frame-creation time (see docs/40 §4.2). A "zero" `Registration` (0
    cells, method="coarse") fed into this function does NOT come out as
    (0, 0) — it comes out as `frame_origin_mm`, because a zero shift only
    means "grid_b's own array origin lines up with the frame array's own
    index (0,0)", which is `frame_origin_mm`, not (0mm, 0mm).
    """
    h, w = grid_b.height, grid_b.width
    if reg.method == "fine":
        coarse_winner = round(reg.rot_deg / 90.0) * 90 % 360
        df = ((reg.rot_deg - coarse_winner + 180) % 360) - 180
        k = (-round(coarse_winner / 90.0)) % 4
        r1, c1, h1, w1 = _rot90_point(0.0, 0.0, h, w, k)
        row0, col0, _d = _rotate_arbitrary_point(r1, c1, h1, w1, df)
    elif reg.method == "fallback-360":
        row0, col0, _d = _rotate_arbitrary_point(0.0, 0.0, h, w, reg.rot_deg)
    else:  # "coarse" — rot_deg is an exact multiple of 90 on this path
        k = (-round(reg.rot_deg / 90.0)) % 4
        row0, col0, h1, w1 = _rot90_point(0.0, 0.0, h, w, k)
    row0 += reg.dy_cells
    col0 += reg.dx_cells

    a = math.cos(math.radians(reg.rot_deg))
    s = math.sin(math.radians(reg.rot_deg))
    ox_mm = grid_b.left * CELL_MM
    oy_mm = grid_b.top * CELL_MM
    fx0_mm = frame_origin_mm[0] + col0 * FRAME_CELL_MM
    fy0_mm = frame_origin_mm[1] + row0 * FRAME_CELL_MM
    tx_mm = fx0_mm - a * ox_mm + s * oy_mm
    ty_mm = fy0_mm - s * ox_mm - a * oy_mm
    return float(reg.rot_deg), float(tx_mm), float(ty_mm)


def robot_mm_to_frame_mm(
    rot_deg: float, tx_mm: float, ty_mm: float, x_mm: float, y_mm: float
) -> tuple[float, float]:
    """Apply a stored registration (`affine_from_registration`'s output) to
    map one point from a robot's own mm space into the frame's shared mm
    space: `frame_mm = R(rot_deg) @ robot_mm + T_mm`."""
    a = math.cos(math.radians(rot_deg))
    s = math.sin(math.radians(rot_deg))
    return a * x_mm - s * y_mm + tx_mm, s * x_mm + a * y_mm + ty_mm


def frame_mm_to_robot_mm(
    rot_deg: float, tx_mm: float, ty_mm: float, fx_mm: float, fy_mm: float
) -> tuple[float, float]:
    """Inverse of `robot_mm_to_frame_mm`: `robot_mm = R(-rot_deg) @ (frame_mm
    - T_mm)` (a rotation matrix's inverse is its transpose — no separate
    determinant/division step needed, unlike the general affine solved for
    `pct_to_mm`)."""
    dx, dy = fx_mm - tx_mm, fy_mm - ty_mm
    a = math.cos(math.radians(-rot_deg))
    s = math.sin(math.radians(-rot_deg))
    return a * dx - s * dy, s * dx + a * dy


def mm_to_home_px(
    origin_mm: tuple[float, float], cell_mm: float, scale: float, x_mm: float, y_mm: float
) -> tuple[float, float]:
    """Frame mm -> published `*_home_px` (docs/40 §4.4) — same density
    convention as today's `*_px` (`HOME_PX_SCALE`, typically 4)."""
    return (
        (x_mm - origin_mm[0]) / cell_mm * scale,
        (y_mm - origin_mm[1]) / cell_mm * scale,
    )


def home_px_to_mm(
    origin_mm: tuple[float, float], cell_mm: float, scale: float, px_x: float, px_y: float
) -> tuple[float, float]:
    """Inverse of `mm_to_home_px`."""
    return (
        px_x / scale * cell_mm + origin_mm[0],
        px_y / scale * cell_mm + origin_mm[1],
    )


# ── Room outline (docs/40 §4.1 — best-effort, may be deferred) ───────────────


def outline_from_mask(
    mask: np.ndarray, cell_mm: float, max_points: int = 60
) -> list[tuple[float, float]]:
    """Trace one connected room mask's outer boundary and simplify it with
    the SAME Douglas-Peucker implementation `coordinator.py` already uses
    for path decimation (`_rdp_simplify` — imported lazily, inside this
    function, to avoid a circular import: `coordinator.py` imports THIS
    module at load time for `decode_grid`/`register`, so this module cannot
    import `coordinator` at load time too). Returns a closed polygon as
    `[(x_mm, y_mm), ...]` in the SAME mm space `mask`'s own cell indices are
    in (caller adds any grid/frame origin). Empty mask -> `[]`.

    Pure numpy/Python, no scipy/opencv (docs/40 §6). Traces the mask's
    *edges* (grid-corner/"crack" coordinates), not pixel centres: an earlier
    version walked Moore-neighbour pixel centres, which cuts a triangle of
    area off every corner (fine for a simple rectangle, but a real bug for
    an L-shape or a thin-necked "I" room — see the regression tests in
    `test_outline_from_mask.py` and the CHANGELOG). Edge tracing is
    pixel-exact by construction: every foreground cell contributes a unit
    boundary edge for each side that borders background/out-of-grid,
    oriented so a clockwise walk keeps the foreground on its right, and the
    resulting polygon's area always equals `cell_count * cell_mm ** 2`
    exactly for any rectilinear region.
    """
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return []
    if ys.size == 1:
        x0, y0 = float(xs[0] * cell_mm), float(ys[0] * cell_mm)
        return [(x0, y0), (x0 + cell_mm, y0), (x0 + cell_mm, y0 + cell_mm), (x0, y0 + cell_mm)]

    height, width = mask.shape
    # 4 cardinal directions, clockwise, in (dx, dy) form (y grows downward,
    # matching the mask's row axis) — E, S, W, N.
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    dir_index = {d: i for i, d in enumerate(dirs)}

    # Build the directed unit-edge graph of the mask's boundary: each
    # foreground cell contributes one directed edge per side that borders
    # background (or the grid edge), between that side's two corners
    # (grid-corner coordinates, cell-index units), oriented clockwise.
    outgoing: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r, c in zip(ys.tolist(), xs.tolist()):
        tl, tr, br, bl = (c, r), (c + 1, r), (c + 1, r + 1), (c, r + 1)
        if r == 0 or not mask[r - 1, c]:
            outgoing.setdefault(tl, []).append(tr)
        if c == width - 1 or not mask[r, c + 1]:
            outgoing.setdefault(tr, []).append(br)
        if r == height - 1 or not mask[r + 1, c]:
            outgoing.setdefault(br, []).append(bl)
        if c == 0 or not mask[r, c - 1]:
            outgoing.setdefault(bl, []).append(tl)

    # Deterministic seed: topmost-then-leftmost foreground pixel's top-left
    # corner. That corner is convex (nothing above or left of it can be
    # foreground), so its top edge is the sole outgoing edge there.
    seed_row = int(ys.min())
    seed_col = int(xs[ys == seed_row].min())
    start = (seed_col, seed_row)

    boundary: list[tuple[int, int]] = [start]
    cur = start
    first_step = outgoing[start][0]
    cur_dir = dir_index[(first_step[0] - cur[0], first_step[1] - cur[1])]
    cur = first_step
    boundary.append(cur)
    used_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    guard = mask.size * 4 + 16  # generous — a real trace never approaches this
    while guard > 0 and cur != start:
        guard -= 1
        candidates = outgoing.get(cur)
        if not candidates:
            break  # shouldn't happen for a closed boundary; stop rather than loop
        back_dir = (cur_dir + 2) % 4  # the direction we just arrived FROM, reversed
        chosen = None
        # Standard "keep turning right whenever possible" rule: starting
        # just past the reverse of our incoming direction, take the first
        # real edge found going clockwise. This is what correctly resolves
        # a vertex shared by two only-diagonally-touching regions (the
        # "bowtie" case) into the boundary of the ring we're actually on,
        # instead of leaking into the other one.
        for k in range(1, 5):
            d = (back_dir + k) % 4
            dx, dy = dirs[d]
            target = (cur[0] + dx, cur[1] + dy)
            if target in candidates and (cur, target) not in used_edges:
                chosen = (target, d)
                break
        if chosen is None:
            break
        nxt, cur_dir = chosen
        used_edges.add((cur, nxt))
        cur = nxt
        boundary.append(cur)

    if len(boundary) > 1 and boundary[-1] == boundary[0]:
        boundary.pop()  # don't double-count the closing vertex

    # Convert to mm and hand to the shared RDP simplifier (dict-of-x/y
    # points, same shape `_rdp_simplify` expects).
    pts = [{"x": float(x * cell_mm), "y": float(y * cell_mm)} for x, y in boundary]
    if len(pts) > max_points:
        from .coordinator import _rdp_simplify  # lazy: see module docstring

        # Binary-search epsilon so the simplified outline fits max_points —
        # `_rdp_simplify` takes a distance tolerance, not a point budget.
        lo, hi = 1.0, max(mask.shape) * cell_mm
        simplified = pts
        for _ in range(20):
            mid = (lo + hi) / 2
            simplified = _rdp_simplify(pts, mid)
            if len(simplified) > max_points:
                lo = mid
            else:
                hi = mid
        pts = simplified
    return [(p["x"], p["y"]) for p in pts]


# ── Wall-corner snap (docs/40 §5.B) ───────────────────────────────────────────


def wall_corner_points(wall_mask: np.ndarray) -> np.ndarray:
    """Enumerates every grid-VERTEX (crack coordinate — the same "corner of a
    cell" coordinate space `outline_from_mask` above traces boundaries in,
    docs/14 rule 1: one coordinate convention for mask geometry, not two)
    where `wall_mask`'s boundary genuinely turns a corner, across the WHOLE
    mask at once — every room's corners, not just one connected region's
    outer ring. This is deliberately a different QUERY than
    `outline_from_mask` (a nearest-corner lookup needs every corner
    anywhere, including interior partition walls and separate wings of the
    home; a single traced ring would miss all of that), but it reuses that
    function's exact underlying idea: classify each grid vertex by which of
    the 4 cells touching it are foreground (wall) — that's the same test
    `outline_from_mask` applies per cell, just read from the vertex's side
    instead of accumulated into a directed-edge walk.

    For a vertex touching cells (top-left, top-right, bottom-left,
    bottom-right), let `count` = how many of those 4 are wall:

    - 0 or 4 -> open space or solid wall interior; not a boundary at all.
    - 1 or 3 -> a plain convex/concave right-angle turn; always a corner.
    - 2, opposite corners only (a "bowtie" — two wall cells touching only
      diagonally, the same ambiguous case `outline_from_mask`'s docstring
      calls out) -> also a corner: two wall strands genuinely meet there.
    - 2, an adjacent pair (top row, bottom row, left column, or right
      column) -> the wall runs straight through this vertex; not a corner.

    Pure vectorised numpy (one padded boolean array + a handful of slices),
    O(cells), no per-pixel Python loop — same performance discipline as
    `decode_grid`/`outline_from_mask`. Returns an `(N, 2)` float array of
    `(x, y)` vertex coordinates in CELL-INDEX units (not mm — multiply by
    `cell_mm` for that), shape `(0, 2)` for an all-empty mask."""
    if not wall_mask.any():
        return np.empty((0, 2), dtype=np.float64)
    h, w = wall_mask.shape
    # False border so edge/corner vertices see a real (missing => background)
    # neighbour instead of needing special-cased bounds checks — same trick
    # `grow_frame_canvas` already relies on elsewhere in this module.
    padded = np.zeros((h + 2, w + 2), dtype=bool)
    padded[1:-1, 1:-1] = wall_mask
    tl = padded[:-1, :-1]
    tr = padded[:-1, 1:]
    bl = padded[1:, :-1]
    br = padded[1:, 1:]
    count = tl.astype(np.int8) + tr.astype(np.int8) + bl.astype(np.int8) + br.astype(np.int8)
    bowtie = (count == 2) & (tl == br) & (tr == bl) & (tl != tr)
    is_corner = (count == 1) | (count == 3) | bowtie
    ys, xs = np.nonzero(is_corner)
    return np.stack([xs.astype(np.float64), ys.astype(np.float64)], axis=1)


def nearest_wall_corner_mm(
    frame: dict[str, Any], x_mm: float, y_mm: float
) -> tuple[float, float] | None:
    """Snaps a query point (frame mm) to the nearest wall-corner vertex in
    `frame["wall_mask"]` — the click-noise fix for cesta B's home-frame side
    of an N-point calibration pair (docs/40 §5.B): an architecturally
    distinctive point (a room corner) is pinpointed exactly from the robot's
    own wall data instead of trusting a hand click's few-pixel imprecision,
    the way docs/39's per-robot flow always has to. Brute-force nearest
    neighbour (vectorised, no k-d tree) — a home frame's corner count is at
    most a few thousand points, so this is comfortably sub-millisecond.
    Returns `None` when the frame has no wall cells at all yet (freshly
    founded, no map decoded since restart) — caller falls back to the raw,
    unsnapped click point rather than failing the whole calibration step."""
    corners = wall_corner_points(frame["wall_mask"])
    if corners.shape[0] == 0:
        return None
    cell_mm = frame.get("cell_mm", FRAME_CELL_MM)
    ox_mm, oy_mm = frame["origin_mm"]
    cx = (x_mm - ox_mm) / cell_mm
    cy = (y_mm - oy_mm) / cell_mm
    d2 = (corners[:, 0] - cx) ** 2 + (corners[:, 1] - cy) ** 2
    i = int(np.argmin(d2))
    vx, vy = corners[i]
    return (float(vx * cell_mm + ox_mm), float(vy * cell_mm + oy_mm))


# ── Fiducial markers for cesta A hardening (docs/40 §5.A.2) ──────────────────
# Deliberately deferred until §5.A.1 (canvas-scale tolerance) and §5.B (cesta
# B N-point calibration) alone weren't enough — this is the "cheap hack" the
# original ratification flagged: invisible markers baked into a home-frame
# snapshot's own 8% padding border, so a floorplan cropped/resized/ROTATED in
# an external editor can still resolve its exact scale+offset+rotation with
# zero clicks, PROVIDED the file's alpha channel survives the edit intact (a
# flattened image, or one re-exported as JPEG, loses the markers — this only
# works for a non-destructive PNG round-trip). Opt-in (`fiducials: true` on
# `anyvac.snapshot_map_as_floorplan`) for exactly that reason.

FIDUCIAL_MARKER_COLORS: dict[str, tuple[int, int, int]] = {
    "tl": (255, 0, 0),  # red
    "tr": (0, 200, 0),  # green
    "bl": (0, 0, 255),  # blue
    "br": (255, 200, 0),  # yellow
}
# Out of 255 — a real photographed/scanned floorplan essentially never has a
# near-fully-transparent pixel, so "low alpha AND close to one of the 4
# marker colours" is a safe, simple signature; not literally 0 so a marker
# surviving an interpolated resize still centres on values very close to
# this rather than snapping to fully opaque on the first blended pixel.
FIDUCIAL_MARKER_ALPHA = 1
FIDUCIAL_ALPHA_MAX = 40  # generous margin for resize interpolation
FIDUCIAL_COLOR_TOLERANCE = 40  # max per-channel-ish distance (squared euclidean below)


def find_fiducial_markers(
    rgba: np.ndarray, colors: dict[str, tuple[int, int, int]] | None = None
) -> dict[str, tuple[float, float] | None]:
    """Scans an RGBA image array (H, W, >=4, uint8) for the invisible
    fiducial markers `services._embed_fiducial_markers` draws into a
    home-frame snapshot. Colour identity — not position — is what tells the
    4 markers apart, so this naturally survives the file being rotated or
    mirrored since the snapshot was taken: the caller pairs whatever it
    finds against the `{id, home_px}` list the snapshot service returned
    when it embedded them, and feeds `{detected_px, known_home_px}` straight
    into the SAME `home_anchors` similarity fit cesta B already computes
    (`homeAnchorFit`, seatfit.ts) — no second, marker-specific geometry
    (docs/14 rule 1).

    Vectorised (no per-pixel Python loop, same discipline as
    `wall_corner_points`): for each of the 4 known colours, mask every pixel
    within `FIDUCIAL_COLOR_TOLERANCE` (squared euclidean, RGB) of it and at
    or below `FIDUCIAL_ALPHA_MAX` alpha, then take the centroid of the
    matching pixels — robust to a moderate resize blurring the marker's
    edges, since the interior pixels still match closely and dominate the
    mean.

    Returns `{id: (x, y) | None}` — pixel centroid in THIS array's own
    coordinate space (+0.5 to land on pixel centres), `None` for a colour no
    pixel matched at all (marker cropped away, or the file lost its alpha
    channel — e.g. re-saved without transparency)."""
    colors = colors or FIDUCIAL_MARKER_COLORS
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        return {mid: None for mid in colors}
    # int32, not int16: a squared 3-channel distance can reach 3*255**2 =
    # 195_075, which overflows int16 (max 32_767) and silently wraps
    # negative — int32's ~2.1e9 ceiling leaves no such risk.
    r = rgba[..., 0].astype(np.int32)
    g = rgba[..., 1].astype(np.int32)
    b = rgba[..., 2].astype(np.int32)
    alpha_ok = rgba[..., 3].astype(np.int32) <= FIDUCIAL_ALPHA_MAX
    results: dict[str, tuple[float, float] | None] = {}
    for mid, (mr, mg, mb) in colors.items():
        dist2 = (r - mr) ** 2 + (g - mg) ** 2 + (b - mb) ** 2
        mask = alpha_ok & (dist2 <= FIDUCIAL_COLOR_TOLERANCE**2)
        ys, xs = np.nonzero(mask)
        results[mid] = None if xs.size == 0 else (float(xs.mean()) + 0.5, float(ys.mean()) + 0.5)
    return results


# ── Cross-robot room identity (docs/40 §4.1) ──────────────────────────────────


def room_identity(
    masks_by_duid: dict[str, dict[int, np.ndarray]], threshold: float = 0.5
) -> dict[tuple[str, int], str]:
    """Assigns one shared `home_room_id` to rooms that are really the same
    physical room seen by different robots, via union-find over every
    cross-robot pair whose IoU (in the FRAME's shared raster — masks must
    already be registered/aligned by the caller) is >= `threshold`. `name` =
    `"<reference-robot-slug>_<segment_id>"` for a room that includes the
    reference robot's own segment, else `"room_<n>"` — never a room NAME
    (docs/40 explicitly keeps card room-rectangles as cosmetic only; this id
    is an internal join key, not a display label). `masks_by_duid` is
    `{duid: {segment_id: aligned_bool_mask}}`; a duid absent or with no
    segments contributes nothing (never raises)."""
    keys: list[tuple[str, int]] = [
        (duid, seg) for duid, segs in masks_by_duid.items() for seg in segs
    ]
    parent: dict[tuple[str, int], tuple[str, int]] = {k: k for k in keys}

    def find(k: tuple[str, int]) -> tuple[str, int]:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: tuple[str, int], b: tuple[str, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    duids = list(masks_by_duid)
    for i, duid_a in enumerate(duids):
        for duid_b in duids[i + 1 :]:
            for seg_a, mask_a in masks_by_duid[duid_a].items():
                for seg_b, mask_b in masks_by_duid[duid_b].items():
                    inter = float(np.logical_and(mask_a, mask_b).sum())
                    if inter == 0:
                        continue
                    union_ = float(np.logical_or(mask_a, mask_b).sum())
                    iou = inter / union_ if union_ else 0.0
                    if iou >= threshold:
                        union((duid_a, seg_a), (duid_b, seg_b))

    groups: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)

    reference_duid = duids[0] if duids else None
    result: dict[tuple[str, int], str] = {}
    room_counter = 0
    for root, members in groups.items():
        ref_member = next((m for m in members if m[0] == reference_duid), None)
        if ref_member is not None:
            name = f"{ref_member[0]}_{ref_member[1]}"
        else:
            room_counter += 1
            name = f"room_{room_counter}"
        for m in members:
            result[m] = name
    return result


# ── Persistence (docs/40 §4.2 — `.storage/anyvac.home_frame`, Store v1) ───────
#
# Pure (de)serialisation only: no `homeassistant.helpers.storage.Store` here
# (this module stays HA-free, see the module docstring) — `coordinator.py`
# owns the actual `Store` object, calls `frames_from_storage`/`_to_storage`
# around `store.async_load()`/`async_delay_save()`, and is the only place
# that decides WHEN a save happens (docs/40 §4.2: debounced, like
# `_paths_store` — never on every 30s poll).
#
# On-disk shape (one JSON-safe dict, exactly docs/40 §4.2):
#   {"frames": {"<frame_id>": {"origin_mm": [x, y], "cell_mm": 50, "scale": 4,
#                               "width": W, "height": H, "epoch": 1,
#                               "floor_mask": "<base64>", "wall_mask": "<base64>",
#                               "robots": {"<duid>": {rot_deg, tx_mm, ty_mm,
#                                                      score, iou, method,
#                                                      map_index, map_sequence,
#                                                      grid_sha1, updated}}}},
#    "robot_frame": {"<duid>": "<frame_id>"}}
#
# In memory, a "frame" dict is the same shape except `floor_mask`/`wall_mask`
# are numpy bool arrays (not base64 strings) and `origin_mm` is a tuple.

_ROBOT_RECORD_STR_KEYS = ("method", "grid_sha1", "updated", "status")
_ROBOT_RECORD_FLOAT_KEYS = ("rot_deg", "tx_mm", "ty_mm", "score", "iou")
_ROBOT_RECORD_INT_KEYS = ("map_index", "map_sequence")


def pack_mask(mask: np.ndarray) -> str:
    """Bool HxW mask -> base64 text (`np.packbits`, row-major) for compact,
    JSON-safe storage — a floor/wall mask packs to 1 bit/cell instead of the
    ~5-8 bytes/cell a plain JSON array of 0/1 (or bool) would take."""
    return base64.b64encode(np.packbits(np.ascontiguousarray(mask, dtype=np.uint8)).tobytes()).decode(
        "ascii"
    )


def unpack_mask(data: str, height: int, width: int) -> np.ndarray:
    """Inverse of `pack_mask` — reshapes back to the given height/width
    (the packed bytes alone don't carry shape, so the frame's own stored
    `height`/`width` must come along for this)."""
    raw = base64.b64decode(data)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))
    return bits[: height * width].reshape(height, width).astype(bool)


def _robot_record_from_storage(data: Any) -> dict[str, Any] | None:
    """Defensively parse one robot's registration record. Returns `None`
    (never raises) on anything malformed — one corrupt robot entry must
    never take the whole store, or coordinator startup, down with it
    (same defensive posture as every `_async_setup` loader in
    `coordinator.py`)."""
    if not isinstance(data, dict):
        return None
    try:
        out: dict[str, Any] = {}
        for key in _ROBOT_RECORD_FLOAT_KEYS:
            if key in data and isinstance(data[key], (int, float)):
                out[key] = float(data[key])
        for key in _ROBOT_RECORD_INT_KEYS:
            if key in data and isinstance(data[key], (int, float)):
                out[key] = int(data[key])
        for key in _ROBOT_RECORD_STR_KEYS:
            if key in data and isinstance(data[key], str):
                out[key] = data[key]
        # rot_deg/tx_mm/ty_mm are the load-bearing geometry — a record
        # missing any of them is unusable, not "partially usable".
        if not all(k in out for k in ("rot_deg", "tx_mm", "ty_mm")):
            return None
        return out
    except Exception:  # noqa: BLE001 - malformed store data, never fatal
        return None


def frame_to_storage(frame: dict[str, Any]) -> dict[str, Any]:
    """One in-memory frame (numpy masks) -> the JSON-safe dict `Store` writes."""
    origin = frame["origin_mm"]
    return {
        "origin_mm": [float(origin[0]), float(origin[1])],
        "cell_mm": float(frame.get("cell_mm", FRAME_CELL_MM)),
        "scale": float(frame.get("scale", HOME_PX_SCALE)),
        "width": int(frame["width"]),
        "height": int(frame["height"]),
        "epoch": int(frame.get("epoch", 1)),
        "floor_mask": pack_mask(frame["floor_mask"]),
        "wall_mask": pack_mask(frame["wall_mask"]),
        "robots": {
            str(duid): dict(rec) for duid, rec in (frame.get("robots") or {}).items()
        },
    }


def frame_from_storage(data: Any) -> dict[str, Any] | None:
    """Inverse of `frame_to_storage`. Returns `None` (never raises) when the
    frame record itself is unusable (bad dimensions, undecodable masks) —
    the caller (`frames_from_storage`) drops that one frame and keeps the
    rest rather than failing the whole load."""
    if not isinstance(data, dict):
        return None
    try:
        width = int(data["width"])
        height = int(data["height"])
        if width <= 0 or height <= 0:
            return None
        origin = data["origin_mm"]
        floor_mask = unpack_mask(data["floor_mask"], height, width)
        wall_mask = unpack_mask(data["wall_mask"], height, width)
        robots_raw = data.get("robots") or {}
        robots: dict[str, Any] = {}
        if isinstance(robots_raw, dict):
            for duid, rec in robots_raw.items():
                parsed = _robot_record_from_storage(rec)
                if parsed is not None:
                    robots[str(duid)] = parsed
        return {
            "origin_mm": (float(origin[0]), float(origin[1])),
            "cell_mm": float(data.get("cell_mm", FRAME_CELL_MM)),
            "scale": float(data.get("scale", HOME_PX_SCALE)),
            "width": width,
            "height": height,
            "epoch": int(data.get("epoch", 1)),
            "floor_mask": floor_mask,
            "wall_mask": wall_mask,
            "robots": robots,
        }
    except Exception:  # noqa: BLE001 - malformed store data, never fatal
        return None


def frames_to_storage(
    frames: dict[str, dict[str, Any]], robot_frame: dict[str, str]
) -> dict[str, Any]:
    """Whole-store snapshot -> the JSON-safe dict `Store.async_delay_save`
    writes (docs/40 §4.2's top-level `{"frames": ..., "robot_frame": ...}`)."""
    return {
        "frames": {str(fid): frame_to_storage(f) for fid, f in frames.items()},
        "robot_frame": {str(duid): str(fid) for duid, fid in robot_frame.items()},
    }


def frames_from_storage(
    data: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Inverse of `frames_to_storage`, for `coordinator._async_setup` to call
    on `await store.async_load()`'s result. Never raises: an empty/`None`
    store (first run), or one with corrupt frames, yields as much usable
    state as can be salvaged rather than blocking startup — exactly the
    posture every other `_async_setup` loader in this integration takes."""
    frames: dict[str, dict[str, Any]] = {}
    frames_raw = data.get("frames") if isinstance(data, dict) else None
    if isinstance(frames_raw, dict):
        for fid, fdata in frames_raw.items():
            parsed = frame_from_storage(fdata)
            if parsed is not None:
                frames[str(fid)] = parsed
    robot_frame: dict[str, str] = {}
    robot_frame_raw = data.get("robot_frame") if isinstance(data, dict) else None
    if isinstance(robot_frame_raw, dict):
        for duid, fid in robot_frame_raw.items():
            if isinstance(fid, str) and fid in frames:
                robot_frame[str(duid)] = fid
    return frames, robot_frame


def grow_frame_canvas(frame: dict[str, Any], new_width: int, new_height: int) -> dict[str, Any]:
    """Return a COPY of `frame` with its `floor_mask`/`wall_mask` grown to
    `new_width` x `new_height` — new cells are `False`, existing content and
    `origin_mm` are untouched (docs/40 §4.1: "rastr se nezmenšuje... origin
    je pevný, rozšíří se jen šířka/výška"). `new_width`/`new_height` must be
    >= the frame's current size in both axes — this function only grows
    toward +x/+y (array index growth); shifting `origin_mm` to grow toward
    -x/-y is the coordinator's job (docs/40 §4.2: bump `epoch`, log a
    WARNING — a deliberate, rare, logged event, never silently done here)."""
    old_h, old_w = frame["height"], frame["width"]
    if new_width < old_w or new_height < old_h:
        raise ValueError(
            f"homeframe.grow_frame_canvas: new size {new_width}x{new_height} is "
            f"smaller than the current {old_w}x{old_h} in some axis — shrinking "
            "a frame is never valid (docs/40 §4.1)"
        )
    grown = dict(frame)
    for key in ("floor_mask", "wall_mask"):
        old_mask = frame[key]
        new_mask = np.zeros((new_height, new_width), dtype=bool)
        new_mask[0:old_h, 0:old_w] = old_mask
        grown[key] = new_mask
    grown["width"] = new_width
    grown["height"] = new_height
    grown["robots"] = dict(frame.get("robots") or {})
    return grown


def _robot_cells_to_frame_cells(
    grid: Grid,
    ys: np.ndarray,
    xs: np.ndarray,
    rot_deg: float,
    tx_mm: float,
    ty_mm: float,
    origin_mm: tuple[float, float],
    cell_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised point-transform shared by `merge_robot_into_frame` and
    `segment_masks_in_frame`: every (row=`ys`, col=`xs`) cell of the robot's
    OWN grid -> its `(row, col)` index in `frame`'s raster, via the SAME mm
    math `robot_mm_to_frame_mm` applies (inlined here for a whole array at
    once instead of one point) followed by the frame's own mm->cell
    division. One implementation, two call sites (docs/14 rule 1)."""
    ox_mm, oy_mm = origin_mm
    x_mm = (grid.left + xs) * CELL_MM
    y_mm = (grid.top + ys) * CELL_MM
    a = math.cos(math.radians(rot_deg))
    s = math.sin(math.radians(rot_deg))
    fx_mm = a * x_mm - s * y_mm + tx_mm
    fy_mm = s * x_mm + a * y_mm + ty_mm
    col = np.round((fx_mm - ox_mm) / cell_mm).astype(np.int64)
    row = np.round((fy_mm - oy_mm) / cell_mm).astype(np.int64)
    return row, col


def segment_masks_in_frame(
    grid: Grid,
    rot_deg: float,
    tx_mm: float,
    ty_mm: float,
    origin_mm: tuple[float, float],
    cell_mm: float,
    width: int,
    height: int,
) -> dict[int, np.ndarray]:
    """One full-frame-sized (`height` x `width`) boolean mask per non-zero
    `grid.room_id` segment, registered into `frame`'s raster the same
    point-based way `merge_robot_into_frame` merges the floor/wall masks —
    the byproduct docs/40 §4.2 point 6 calls `outline_home_px`/
    `home_room_id`: full-frame sized (not cropped to each room's own bbox)
    so `room_identity`'s plain `np.logical_and` IoU and `outline_from_mask`
    both work on them unmodified, at the cost of one small array per room
    per registration event (never per poll — the coordinator caches this
    dict, in memory only, alongside the frame it was computed against)."""
    out: dict[int, np.ndarray] = {}
    seg_ids = [int(v) for v in np.unique(grid.room_id) if v != 0]
    for seg_id in seg_ids:
        ys, xs = np.nonzero(grid.room_id == seg_id)
        if ys.size == 0:
            continue
        row, col = _robot_cells_to_frame_cells(
            grid, ys, xs, rot_deg, tx_mm, ty_mm, origin_mm, cell_mm
        )
        ok = (row >= 0) & (row < height) & (col >= 0) & (col < width)
        if not ok.any():
            continue
        mask = np.zeros((height, width), dtype=bool)
        mask[row[ok], col[ok]] = True
        out[seg_id] = mask
    return out


def merge_robot_into_frame(
    frame: dict[str, Any], grid: Grid, rot_deg: float, tx_mm: float, ty_mm: float
) -> dict[str, Any]:
    """Merge one robot's decoded floor/wall masks into `frame`'s own raster
    (docs/40 §4.1-4.2), returning a NEW frame dict — `frame` itself is never
    mutated (copy-on-write, same contract as `grow_frame_canvas`, so a
    concurrent executor job still holding the old frame object is unaffected
    by a later replacement — see `coordinator.py`'s home-frame concurrency
    note).

    Deliberately POINT-BASED, not a second `_rotate_arbitrary`/`np.rot90`
    resample of the whole grid onto the frame raster: every True cell of the
    robot's OWN floor/wall mask is converted to its mm position (the same
    `cell_to_mm` arithmetic `register()`'s callers already use), rotated/
    shifted into the frame's shared mm space via the SAME formula
    `robot_mm_to_frame_mm` applies (inlined here, vectorised, for every cell
    at once), converted to a frame cell index, and OR-ed into the frame's own
    floor/wall masks. This sidesteps every pivot/canvas-convention pitfall
    `affine_from_registration` above had to work so hard to get right for
    `register()`'s OWN internal correlation step (docs/40 §6): merging never
    needs to reproduce that search machinery, only apply its already-clean,
    final `(rot_deg, tx_mm, ty_mm)` result to plain points.

    Handles frame growth in every direction: a robot's rotated footprint can
    land partially beyond the frame's current `width`/`height` (grown via
    `grow_frame_canvas`, `origin_mm` untouched) OR at a negative frame-cell
    index (an origin shift — `origin_mm` moves and `epoch` is bumped, so a
    caller can tell a growth-with-shift apart from a growth-only one and log
    the docs/40 §4.2 WARNING; this function itself does no logging, staying
    HA-free like the rest of the module)."""
    ox_mm, oy_mm = frame["origin_mm"]
    cell_mm = frame.get("cell_mm", FRAME_CELL_MM)
    old_h, old_w = int(frame["height"]), int(frame["width"])

    ys, xs = np.nonzero(grid.floor | grid.wall)
    if ys.size == 0:
        merged = dict(frame)
        merged["floor_mask"] = frame["floor_mask"].copy()
        merged["wall_mask"] = frame["wall_mask"].copy()
        merged["robots"] = dict(frame.get("robots") or {})
        return merged

    # mm positions of every source cell, rotated/shifted into the frame's
    # shared mm space and converted to a frame cell index — shared with
    # `segment_masks_in_frame` (docs/14 rule 1).
    row, col = _robot_cells_to_frame_cells(
        grid, ys, xs, rot_deg, tx_mm, ty_mm, frame["origin_mm"], cell_mm
    )

    min_col, max_col = int(col.min()), int(col.max())
    min_row, max_row = int(row.min()), int(row.max())
    shift_x = max(0, -min_col)
    shift_y = max(0, -min_row)
    new_width = shift_x + max(old_w, max_col + 1)
    new_height = shift_y + max(old_h, max_row + 1)

    if shift_x or shift_y:
        floor_new = np.zeros((new_height, new_width), dtype=bool)
        wall_new = np.zeros((new_height, new_width), dtype=bool)
        floor_new[shift_y : shift_y + old_h, shift_x : shift_x + old_w] = frame["floor_mask"]
        wall_new[shift_y : shift_y + old_h, shift_x : shift_x + old_w] = frame["wall_mask"]
        merged = dict(frame)
        merged["floor_mask"] = floor_new
        merged["wall_mask"] = wall_new
        merged["origin_mm"] = (ox_mm - shift_x * cell_mm, oy_mm - shift_y * cell_mm)
        merged["width"] = new_width
        merged["height"] = new_height
        merged["epoch"] = int(frame.get("epoch", 1)) + 1
        merged["robots"] = dict(frame.get("robots") or {})
    else:
        merged = grow_frame_canvas(frame, new_width, new_height)

    dst_col = col + shift_x
    dst_row = row + shift_y
    is_floor = grid.floor[ys, xs]
    is_wall = grid.wall[ys, xs]
    merged["floor_mask"][dst_row[is_floor], dst_col[is_floor]] = True
    merged["wall_mask"][dst_row[is_wall], dst_col[is_wall]] = True
    return merged
