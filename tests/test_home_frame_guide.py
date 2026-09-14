"""Tests for docs/40 Fáze 2.3 — `frame: "home"` on `anyvac.export_map_guide`:
real room shapes (`outline_home_px`) instead of one vacuum's `bbox_px`
rectangles, and the crop-box policy (`_home_frame_occupied_crop_px`) shared
with `snapshot_map_as_floorplan`'s own `frame: "home"` composite (docs/14
rule 1 — tested once in `test_home_frame_composite.py`'s `_select_home_frame`
suite already covers the frame-selection half of this; this file covers the
drawing half).

Same convention as `test_export_map_guide.py`: only the pure/blocking
pieces factored OUT of the real async handler (a closure inside
`async_register_services`, doing real file I/O against a running Home
Assistant instance) are exercised here.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from custom_components.anyvac.services import (
    _guide_room_outline_polygons,
    _home_frame_occupied_crop_px,
    _render_guide_layer,
)

# ── _guide_room_outline_polygons ─────────────────────────────────────────────


def test_outline_polygon_translated_to_crop_local_coordinates() -> None:
    rooms = [{"name": "Kitchen", "outline_home_px": [[10.0, 20.0], [30.0, 20.0], [30.0, 40.0]]}]
    polys = _guide_room_outline_polygons(rooms, (5.0, 5.0, 100.0, 100.0))
    assert len(polys) == 1
    pts, name = polys[0]
    assert name == "Kitchen"
    assert pts == [(5.0, 15.0), (25.0, 15.0), (25.0, 35.0)]


def test_rooms_without_outline_are_skipped() -> None:
    rooms = [{"name": "No outline yet", "outline_home_px": None}, {"name": "Empty", "outline_home_px": []}]
    assert _guide_room_outline_polygons(rooms, (0.0, 0.0, 10.0, 10.0)) == []


def test_degenerate_outline_with_fewer_than_three_points_is_skipped() -> None:
    rooms = [{"name": "Sliver", "outline_home_px": [[1.0, 1.0], [2.0, 2.0]]}]
    assert _guide_room_outline_polygons(rooms, (0.0, 0.0, 10.0, 10.0)) == []


def test_malformed_outline_point_does_not_raise() -> None:
    rooms = [{"name": "Bad", "outline_home_px": [[1.0, None], [2.0, 2.0], [3.0, 3.0]]}]
    assert _guide_room_outline_polygons(rooms, (0.0, 0.0, 10.0, 10.0)) == []


def test_non_dict_room_entries_are_skipped_without_raising() -> None:
    assert _guide_room_outline_polygons([None, "not a room", 42], (0.0, 0.0, 10.0, 10.0)) == []


# ── _render_guide_layer(..., polygons=...) ───────────────────────────────────


def test_render_rooms_layer_draws_polygon_outline() -> None:
    polygons = [([(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0)], "Kitchen")]
    png = _render_guide_layer("rooms", (10, 10), polygons=polygons, labels=False)
    assert png is not None
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # A point on the drawn top edge must be the magenta "rooms" colour.
    assert img.getpixel((5, 2))[:3] == (255, 0, 255)
    # The polygon's interior (not on any edge) must stay transparent — an
    # outline, not a filled shape.
    assert img.getpixel((5, 5))[3] == 0


def test_render_rooms_layer_with_no_polygons_and_no_rects_returns_none() -> None:
    assert _render_guide_layer("rooms", (10, 10), polygons=[], labels=False) is None


def test_render_rooms_layer_polygons_take_priority_over_rects() -> None:
    # A caller only ever passes one of the two in practice, but polygons
    # must win if both are somehow given — it's the more specific shape.
    polygons = [([(1.0, 1.0), (5.0, 1.0), (5.0, 5.0)], None)]
    rects = [((0.0, 0.0, 9.0, 9.0), None)]
    png = _render_guide_layer("rooms", (10, 10), polygons=polygons, rects=rects, labels=False)
    assert png is not None
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # A corner that's inside the rect outline but nowhere near the polygon
    # must stay untouched if polygons (not rects) were actually drawn.
    assert img.getpixel((0, 0))[3] == 0


# ── _home_frame_occupied_crop_px ─────────────────────────────────────────────


def test_occupied_crop_px_scales_and_pads_the_bbox() -> None:
    floor = np.zeros((20, 20), dtype=bool)
    floor[8:12, 8:12] = True
    wall = np.zeros((20, 20), dtype=bool)
    box = _home_frame_occupied_crop_px({"floor_mask": floor, "wall_mask": wall, "scale": 4.0})
    left, top, right, bottom = box
    assert left < 32 and top < 32  # tight bbox would be (32, 32, 48, 48)
    assert right > 48 and bottom > 48


def test_occupied_crop_px_with_no_occupied_cells_falls_back_to_full_canvas() -> None:
    floor = np.zeros((5, 5), dtype=bool)
    wall = np.zeros((5, 5), dtype=bool)
    box = _home_frame_occupied_crop_px({"floor_mask": floor, "wall_mask": wall, "scale": 4.0})
    left, top, right, bottom = box
    assert left == 0 and top == 0
    assert right == 20 and bottom == 20  # 5 * scale 4, unpadded (nothing to pad around)
