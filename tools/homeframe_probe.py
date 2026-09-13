"""Offline verification tool for docs/40 "home frame" — Fáze 0.

Pure Python + numpy, NO Home Assistant, NO opencv/scipy/scikit-image
(docs/40 §6 "co NEDĚLAT"). Takes raw Roborock map dumps produced by
``anyvac.dump_raw_map`` (config/www/anyvac/debug/*.bin, or the copies a user
placed under ``anyvac/tools/samples/``) and:

1. Decodes each one's IMAGE block directly from bytes into numpy masks
   (`decode_grid`) — a from-scratch reference decoder, independent of
   `vacuum_map_parser_roborock`'s own PIL-rendered image (which redraws path/
   dock/robot on top and, on some models, recolours segments — docs/40 §2).
2. Self-tests that decoder against the real library parser's own `Room.x0..y1`
   bboxes (`self_test`) — the whole exercise is worthless if this doesn't
   match to the cell.
3. Registers every other map's floor mask onto the first (reference) map's,
   via coarse 4×90° + fine ±6°/1° FFT cross-correlation (`register`), scored
   by `covered` (fraction of B's own floor recovered — partial-map aware) and
   `iou`.
4. Writes a visual overlay PNG per pair (reference in grey, the registered
   map in colour, semi-transparent) under `anyvac/tools/samples/out/`.

`decode_grid` here is the literal seed for the Fáze 1 production module
(`custom_components/anyvac/homeframe.py`) — written with zero dependency on
this file (or on HA) so it moves there unchanged. `register`/
`kabsch_from_names` are likewise structured to move over as-is.

Usage:
    python homeframe_probe.py ref.bin other1.bin [other2.bin ...]

The first file is the registration REFERENCE (docs/40 §4.2: pick the most
complete map — usually the vacuum with the fullest explored floor).
"""

from __future__ import annotations

import math
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# 1 raw grid cell = 50 mm (docs/40 §2) — fixed by the Roborock map format
# itself, not something that changes per-robot or per-map.
CELL_MM = 50

# vacuum_map_parser_roborock.map_data_parser.RoborockBlockType.IMAGE.value —
# hardcoded here (rather than imported) so this tool has NO import-time
# dependency on the library at all; `self_test` below is what cross-checks
# against it, deliberately kept separate from decoding.
_BLOCK_TYPE_IMAGE = 2


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


@dataclass
class Grid:
    """One robot's raw map, decoded straight from bytes (docs/40 §2).

    `room_id`/`floor`/`wall` are HxW numpy arrays in the map's OWN raw pixel
    grid (row-major, `idx = x + width*y`, NOT y-flipped — matches the byte
    layout exactly, no image-coordinate flip applied). `top`/`left` are the
    grid's own origin, in CELLS: cell `(ix, iy)`'s absolute position is
    `((left + ix) * CELL_MM, (top + iy) * CELL_MM)` mm — see `cell_to_mm`.
    """

    room_id: np.ndarray  # uint8 HxW, 0 = not a room cell, else segment id
    floor: np.ndarray  # bool HxW — room_id > 0 (registration only needs this)
    wall: np.ndarray  # bool HxW — bonus, for future outline/snap use
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
    source, and re-verified live against the installed library in this
    session — see `self_test`). Do NOT "clean up" the arithmetic without
    re-checking against that source; a single off-by-one here silently
    corrupts every downstream mm coordinate.
    """
    if len(raw) < 0x14:
        raise ValueError("homeframe_probe.decode_grid: blob too short for a map header")

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
                    "homeframe_probe.decode_grid: IMAGE block header/data out of bounds"
                )
            image_top = _i32(raw, header_end - 16)
            image_left = _i32(raw, header_end - 12)
            image_height = _i32(raw, header_end - 8)
            image_width = _i32(raw, header_end - 4)
            image_data = raw[header_end : header_end + block_data_length]
            break  # nothing else in the blob is needed for registration
        pos = header_end + block_data_length

    if image_data is None or not image_width or not image_height:
        raise ValueError("homeframe_probe.decode_grid: no IMAGE block found in raw map")

    expected = image_width * image_height
    if len(image_data) < expected:
        raise ValueError(
            f"homeframe_probe.decode_grid: IMAGE block too short "
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


def self_test(grid: Grid, lib_rooms: dict[int, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Compare each segment's bbox computed from `grid.room_id` against the
    SAME segment's `Room.x0..y1` from the real library parser's own output
    (`MapData.rooms`) — must match to the cell (docs/40 §2/§3). `lib_rooms`
    is `{segment_number: Room}` exactly as `vacuum_map_parser_base` produces
    it. Returns `(all_match, rows)` — `all_match=False` means the decoder
    must NOT be trusted, and Fáze 1 must not start (docs/40 §7 Fáze 0 stop
    condition)."""
    rows: list[dict[str, Any]] = []
    all_match = True
    for seg_id, room in sorted(lib_rooms.items()):
        mask = grid.room_id == seg_id
        if not mask.any():
            rows.append(
                {"segment_id": seg_id, "match": False, "reason": "no cells for this segment in grid"}
            )
            all_match = False
            continue
        ys, xs = np.nonzero(mask)
        gx0, gy0 = cell_to_mm(grid, int(xs.min()), int(ys.min()))
        gx1, gy1 = cell_to_mm(grid, int(xs.max()), int(ys.max()))
        lib_bbox = (
            getattr(room, "x0", None),
            getattr(room, "y0", None),
            getattr(room, "x1", None),
            getattr(room, "y1", None),
        )
        grid_bbox = (gx0, gy0, gx1, gy1)
        match = lib_bbox == grid_bbox
        if not match:
            all_match = False
        rows.append(
            {"segment_id": seg_id, "match": match, "grid_bbox": grid_bbox, "lib_bbox": lib_bbox}
        )
    return all_match, rows


# ── Registration (docs/40 §3.2-3.3, §4.2 krok 3) ──────────────────────────────


@dataclass
class Registration:
    rot_deg: int
    dy_cells: int
    dx_cells: int
    covered: float  # |B∩A| / |B| — partial-map aware (B never has to cover all of A)
    iou: float
    method: str  # "coarse" or "fine"
    seconds: float


def _rotate_arbitrary(mask: np.ndarray, deg: float) -> np.ndarray:
    """Nearest-neighbour rotation of a boolean mask by `deg` degrees about its
    own centre, via inverse coordinate rotation into an enlarged (diagonal +
    margin) square canvas so nothing clips — no scipy/PIL dependency. Exact
    for `deg` a multiple of 90 up to float rounding; `register` uses
    `np.rot90` instead for those (exact, faster) and this only for genuine
    fine-sweep angles."""
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
    # Inverse-map each output pixel back into the source mask (standard
    # rotate-by-sampling, avoids holes that forward-mapping would leave).
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
    is linear, not circular (docs/40 §3.2 — a wrap-around match would let
    unrelated far corners of two floor plans "align"). `dy`/`dx` are B's
    offset relative to A (add to B's own coordinates to land on A's)."""
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
    """`covered` (= |B∩A| / |B|, partial-map aware — B never has to cover all
    of A's already-explored floor) and `iou` for `fb` shifted by (dy, dx)
    onto `fa`'s canvas."""
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
    """Register `mask_b` (a floor bool mask, robot B, ITS OWN raw grid, cell
    size CELL_MM) onto `mask_ref` (robot A / the frame). Coarse 4×90° pass
    (exact `np.rot90`, no interpolation loss) + a fine ±6°/1° sweep around
    the coarse winner (nearest-neighbour `_rotate_arbitrary`), each scored by
    `covered` then `iou` as tiebreak — matches the validated approach from
    the docs/40 design spike (rotation always correctly distinguished at
    ~1.0 vs ~0.36 score in that spike's synthetic tests). No scipy — see
    docs/40 §6.
    """
    t0 = time.perf_counter()
    fa = mask_ref.astype(np.float32)
    fb_base = mask_b.astype(np.float32)

    # `_rotate_arbitrary(m, +θ)` and `np.rot90(m, k=-θ/90)` are THE SAME "undo a
    # θ° rotation" operation (verified: for exact multiples of 90 they agree
    # pixel-for-pixel) — never mix them by re-deriving a combined angle from
    # `fb_base` with one function and the coarse winner with the other; that
    # sign mismatch was caught by this file's own synthetic validation (93°/
    # 86° cases landing on the wrong quadrant) before being fixed here. The
    # fine sweep instead composes its small delta ON TOP of the winning
    # coarse candidate's ALREADY-rotated mask, so both stages always agree.
    best: Registration | None = None
    best_fb_coarse: np.ndarray | None = None
    for ang0 in coarse:
        # Exact `np.rot90` for multiples of 90° (no interpolation loss, and
        # what `register_with_fallback`'s default 4-angle coarse pass always
        # hits); `_rotate_arbitrary` for anything else (only reached by
        # `register_with_fallback`'s 360°@2° last-resort sweep).
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
            continue  # already scored as the coarse candidate above
        fb_fine = _rotate_arbitrary(best_fb_coarse, df)  # further undo of df° on top
        dy, dx = _fft_best_shift(fa, fb_fine)
        covered, iou = _score(fa, fb_fine, dy, dx)
        cand = Registration((coarse_winner + df) % 360, dy, dx, covered, iou, "fine", 0.0)
        if best is None or (cand.covered, cand.iou) > (best.covered, best.iou):
            best = cand

    assert best is not None  # coarse always has >=1 candidate
    best.seconds = time.perf_counter() - t0
    return best


def register_with_fallback(
    mask_b: np.ndarray,
    mask_ref: np.ndarray,
    *,
    threshold_covered: float = 0.6,
    threshold_iou: float = 0.4,
) -> Registration:
    """`register` with the docs/40 §4.2 default gate; if coarse+fine both miss
    the gate, falls back to a full 360°@2° sweep on a 2x-downsampled grid
    (cheaper per-angle, coarser result — a last resort, not the normal path)
    before giving up."""
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
    transform (rotation + translation, NO scale — robots share one physical
    mm scale, docs/40 §3) via Kabsch. Only ever a cross-check column next to
    `register`'s mask-based result, never the primary source — needs >=2
    shared names and is easily thrown off by one mismatched name. Returns
    `(rot_deg, (tx_mm, ty_mm))` mapping B's mm space onto the reference's, or
    None when fewer than 2 names are shared."""
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


# ── Visual overlay (docs/40 §3.2 point 4 — "uživatel to chce vidět") ──────────


def save_overlay_png(
    mask_ref: np.ndarray,
    mask_b: np.ndarray,
    reg: Registration,
    out_path: Path,
) -> None:
    """A greyscale render of the reference floor, with the registered `mask_b`
    (after applying `reg`) drawn semi-transparently in colour on top — a
    quick human sanity check that the registration actually lines up real
    rooms, not just a numeric score. Uses PIL (already an HA core dependency,
    same as every other image-writing service in this integration)."""
    from PIL import Image

    h, w = mask_ref.shape
    # `_rotate_arbitrary(m, +θ)` and `np.rot90(m, k=-θ/90)` are the SAME "undo
    # a θ° rotation" operation (see `register`'s docstring) — use `np.rot90`
    # for exact multiples (exact, no interpolation) and `_rotate_arbitrary`
    # with the SAME sign otherwise.
    if reg.rot_deg % 90 == 0:
        rotated_b = np.rot90(mask_b, k=(-(reg.rot_deg // 90)) % 4)
    else:
        rotated_b = _rotate_arbitrary(mask_b, reg.rot_deg)

    canvas = np.zeros((h, w, 4), dtype=np.uint8)
    canvas[mask_ref, 0:3] = 160  # reference floor: mid grey
    canvas[mask_ref, 3] = 255

    ys, xs = np.nonzero(rotated_b)
    ys2, xs2 = ys + reg.dy_cells, xs + reg.dx_cells
    ok = (ys2 >= 0) & (ys2 < h) & (xs2 >= 0) & (xs2 < w)
    ys2, xs2 = ys2[ok], xs2[ok]
    # Additive-ish colour blend so overlap with the grey reference reads as a
    # visibly different (yellow-ish) tone rather than fully occluding it.
    canvas[ys2, xs2, 0] = 255
    canvas[ys2, xs2, 1] = np.clip(canvas[ys2, xs2, 1].astype(int) + 200, 0, 255).astype(np.uint8)
    canvas[ys2, xs2, 2] = 40
    canvas[ys2, xs2, 3] = 200

    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(canvas, mode="RGBA").save(out_path)


# ── CLI ────────────────────────────────────────────────────────────────────────


def _load_lib_rooms(raw: bytes) -> dict[int, Any]:
    """Parse `raw` with the REAL library parser, for `self_test` only — never
    used for registration itself (docs/40 §2: "neber masky z map_data.image.
    data ... jediný zdroj = raw grid")."""
    from vacuum_map_parser_base.config.color import ColorsPalette
    from vacuum_map_parser_base.config.drawable import Drawable
    from vacuum_map_parser_base.config.image_config import ImageConfig
    from vacuum_map_parser_base.config.size import Sizes
    from vacuum_map_parser_roborock.map_data_parser import RoborockMapDataParser

    parser = RoborockMapDataParser(ColorsPalette(), Sizes(), [d for d in Drawable], ImageConfig(), [])
    map_data = parser.parse(raw)
    return dict(map_data.rooms or {})


def _report_pair(name_b: str, name_ref: str, grid_b: Grid, grid_ref: Grid, out_dir: Path) -> None:
    reg = register_with_fallback(grid_b.floor, grid_ref.floor)
    print(
        f"  {name_b} -> {name_ref}: rot={reg.rot_deg:>3}°  "
        f"shift=({reg.dx_cells * CELL_MM:+d}, {reg.dy_cells * CELL_MM:+d}) mm  "
        f"covered={reg.covered:.3f}  iou={reg.iou:.3f}  method={reg.method}  "
        f"{reg.seconds * 1000:.0f} ms"
    )
    out_path = out_dir / f"overlay_{name_b}_onto_{name_ref}.png"
    try:
        save_overlay_png(grid_ref.floor, grid_b.floor, reg, out_path)
        print(f"    overlay -> {out_path}")
    except Exception as err:  # noqa: BLE001 - overlay is a nice-to-have, never fatal
        print(f"    overlay FAILED ({err}) — registration numbers above are still valid")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    paths = [Path(p) for p in argv[1:]]
    for p in paths:
        if not p.is_file():
            print(f"error: not a file: {p}")
            return 2

    grids: dict[str, Grid] = {}
    print("=== decode + self-test ===")
    for p in paths:
        name = p.stem
        raw = p.read_bytes()
        t0 = time.perf_counter()
        grid = decode_grid(raw)
        decode_s = time.perf_counter() - t0
        grids[name] = grid
        try:
            lib_rooms = _load_lib_rooms(raw)
            ok, rows = self_test(grid, lib_rooms)
        except Exception as err:  # noqa: BLE001 - a self-test crash is itself a finding
            ok, rows = False, [{"segment_id": None, "match": False, "reason": str(err)}]
        status = "OK" if ok else "MISMATCH — decoder NOT trustworthy for this file"
        print(
            f"{name}: {grid.width}x{grid.height} cells, top={grid.top} left={grid.left}, "
            f"map_index={grid.map_index} map_sequence={grid.map_sequence}, "
            f"floor_cells={int(grid.floor.sum())}, decode={decode_s * 1000:.0f}ms  "
            f"self-test: {status}"
        )
        for row in rows:
            if not row["match"]:
                print(f"    segment {row['segment_id']}: {row}")

    if len(grids) < 2:
        print("\nOnly one file given — nothing to register against.")
        return 0

    ref_name = next(iter(grids))
    ref_grid = grids[ref_name]
    print(f"\n=== registration (reference = {ref_name}) ===")
    out_dir = Path(__file__).parent / "samples" / "out"
    for name, grid in grids.items():
        if name == ref_name:
            continue
        _report_pair(name, ref_name, grid, ref_grid, out_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
