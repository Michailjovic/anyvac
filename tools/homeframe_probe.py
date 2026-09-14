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

`decode_grid`/`register`/`register_with_fallback`/`kabsch_from_names` used to
be defined here; as of Fáze 1 they live in the production module
(`custom_components/anyvac/homeframe.py`, itself the promoted, unchanged
Fáze 0 code) and this tool imports them from there (docs/14 rule 1 — one
implementation, never a second copy of the decoder/registration logic). That
module has zero Home Assistant import at module scope, so it's loaded here
by file path (`_load_homeframe_module` below) rather than via
`custom_components.anyvac.homeframe` — importing THAT way would first run
`custom_components/anyvac/__init__.py`, which does import Home Assistant,
defeating this tool's whole "no HA needed" point.

Usage:
    python homeframe_probe.py ref.bin other1.bin [other2.bin ...]

The first file is the registration REFERENCE (docs/40 §4.2: pick the most
complete map — usually the vacuum with the fullest explored floor).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


def _load_homeframe_module():
    """Load `custom_components/anyvac/homeframe.py` by file path, bypassing
    the `custom_components.anyvac` PACKAGE (whose `__init__.py` imports
    Home Assistant) — see the module docstring above."""
    path = Path(__file__).parent.parent / "custom_components" / "anyvac" / "homeframe.py"
    spec = importlib.util.spec_from_file_location("anyvac_homeframe", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register in sys.modules BEFORE exec: the dataclass decorator (used by
    # `Grid`/`Registration` in homeframe.py) looks its own defining module up
    # via `sys.modules[cls.__module__]` and fails with a confusing
    # AttributeError if it isn't there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_hf = _load_homeframe_module()
Grid = _hf.Grid
Registration = _hf.Registration
decode_grid = _hf.decode_grid
cell_to_mm = _hf.cell_to_mm
register = _hf.register
register_with_fallback = _hf.register_with_fallback
kabsch_from_names = _hf.kabsch_from_names
_rotate_arbitrary = _hf._rotate_arbitrary
CELL_MM = _hf.CELL_MM


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
