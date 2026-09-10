"""Tests for `anyvac.export_map_guide` (docs/37, ratified 2026-09-10).

Field context: building a custom floorplan by hand (GIMP etc.) means tracing
furniture from the negative space inside the vacuum's own cleaning path — today
that means eyeballing it off the raw map picture. All the data needed already
exists in the SAME pixel space as `snapshot_map_as_floorplan`'s crop
(`rooms[].bbox_px`, `path_dry_px`, `path_wet_px` — all produced by the same
`_px_point(p, aff)` conversion in coordinator.py, docs/37 §3). This service
exports that geometry as transparent PNG tracing layers instead.

Per docs/37 §9: pure helpers (filename, coordinate transform, `px_per_mm`) get
direct unit tests; one real PIL round-trip exercises the actual drawing code,
since that's the only test that would catch a flipped axis or an origin
off-by-one.
"""

from __future__ import annotations

import io

from PIL import Image

from custom_components.anyvac.coordinator import AnyVacCoordinator, AnyVacDevice
from custom_components.anyvac.services import (
    _guide_filename,
    _guide_path_segments,
    _guide_point,
    _guide_room_rects,
    _render_guide_layer,
)

# ── _guide_filename ──────────────────────────────────────────────────────────


def test_guide_filename_is_slugified_per_layer() -> None:
    assert _guide_filename("S6 MaxV Ultra!", "rooms") == "anyvac_guide_s6_maxv_ultra_rooms.png"
    assert _guide_filename("s6_kitchen_map_0", "dry") == "anyvac_guide_s6_kitchen_map_0_dry.png"
    assert _guide_filename("S8", "wet") == "anyvac_guide_s8_wet.png"


def test_guide_filename_is_always_png() -> None:
    # Unlike `_floorplan_filename`, there's no source content-type to mirror —
    # the canvas is always drawn RGBA and saved as PNG.
    assert _guide_filename("S6", "rooms").endswith(".png")


def test_guide_filename_empty_name_falls_back_to_vacuum() -> None:
    assert _guide_filename("", "rooms") == "anyvac_guide_vacuum_rooms.png"
    assert _guide_filename("!!!", "dry") == "anyvac_guide_vacuum_dry.png"


def test_guide_filename_is_stable_for_the_same_inputs() -> None:
    assert _guide_filename("S8 MaxV Ultra", "wet") == _guide_filename("S8 MaxV Ultra", "wet")


# ── Coordinate transform (docs/37 §3/§6) ─────────────────────────────────────
# guide_px = (px.x - crop.x0, px.y - crop.y0); a point outside the crop is
# dropped and breaks the path segment there rather than bridging the gap.

CROP = (100.0, 200.0, 300.0, 400.0)  # x0, y0, x1, y1


def test_guide_point_inside_crop_shifts_by_origin() -> None:
    assert _guide_point({"x": 150.0, "y": 250.0}, CROP) == (50.0, 50.0)
    # On the crop's own boundary counts as inside (inclusive).
    assert _guide_point({"x": 100.0, "y": 200.0}, CROP) == (0.0, 0.0)
    assert _guide_point({"x": 300.0, "y": 400.0}, CROP) == (200.0, 200.0)


def test_guide_point_outside_crop_is_dropped() -> None:
    assert _guide_point({"x": 99.0, "y": 250.0}, CROP) is None
    assert _guide_point({"x": 150.0, "y": 401.0}, CROP) is None
    assert _guide_point({"x": None, "y": 250.0}, CROP) is None


def test_guide_path_segments_breaks_at_crop_boundary() -> None:
    # One raw segment that dips outside the crop in the middle must become
    # TWO drawn segments, not one straight line bridging the gap.
    raw = [
        {"x": 150.0, "y": 250.0},  # inside
        {"x": 200.0, "y": 250.0},  # inside
        {"x": 50.0, "y": 250.0},  # OUTSIDE -> break
        {"x": 250.0, "y": 300.0},  # inside again -> new segment
    ]
    out = _guide_path_segments([raw], CROP)
    assert out == [
        [(50.0, 50.0), (100.0, 50.0)],
        [(150.0, 100.0)],
    ]


def test_guide_path_segments_drops_fully_empty_input() -> None:
    assert _guide_path_segments([], CROP) == []
    # A segment entirely outside the crop contributes nothing.
    assert _guide_path_segments([[{"x": 0.0, "y": 0.0}]], CROP) == []


def test_guide_room_rects_skips_rooms_without_a_usable_bbox() -> None:
    rooms = [
        {"name": "Kitchen", "bbox_px": {"x0": 120.0, "y0": 220.0, "x1": 180.0, "y1": 280.0}},
        {"name": "no bbox"},
        {"name": "partial", "bbox_px": {"x0": None, "y0": 1, "x1": 2, "y1": 3}},
    ]
    rects = _guide_room_rects(rooms, CROP)
    assert rects == [((20.0, 20.0, 80.0, 80.0), "Kitchen")]


# ── px_per_mm (docs/37 §5) ────────────────────────────────────────────────


def _coord_with_calibration_points(duid: str, calibration_points: object) -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.data = {
        duid: AnyVacDevice(
            duid=duid, slug=duid, name=duid,
            data={"calibration_points": calibration_points},
        )
    }
    return coord


def test_px_per_mm_from_known_affine() -> None:
    # Pure 2x uniform scale, no rotation: map = 2 * vacuum. Any 3 non-collinear
    # points fully determine the affine; px_per_mm must recover the scale
    # factor exactly (sqrt(|det|) of the linear part == 2).
    calib = [
        {"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}},
        {"vacuum": {"x": 100, "y": 0}, "map": {"x": 200, "y": 0}},
        {"vacuum": {"x": 0, "y": 100}, "map": {"x": 0, "y": 200}},
    ]
    coord = _coord_with_calibration_points("s6", calib)
    assert coord.px_per_mm("s6") == 2.0


def test_px_per_mm_none_without_calibration() -> None:
    coord = _coord_with_calibration_points("s6", None)
    assert coord.px_per_mm("s6") is None
    coord2 = _coord_with_calibration_points("s6", [])
    assert coord2.px_per_mm("s6") is None


def test_px_per_mm_none_for_unknown_duid() -> None:
    coord = _coord_with_calibration_points("s6", None)
    assert coord.px_per_mm("s7") is None


# ── PIL round-trip (docs/37 §9 point 4) ──────────────────────────────────────
# The only test that would actually catch a flipped axis or an origin
# off-by-one — everything above stops at plain tuples.


def test_render_guide_layer_draws_a_line_on_a_transparent_canvas() -> None:
    canvas = (50, 60)
    segments = [[(5.0, 5.0), (5.0, 55.0)]]  # a vertical line near the left edge
    png = _render_guide_layer("dry", canvas, segments=segments, stroke_px=6)
    assert png is not None
    with Image.open(io.BytesIO(png)) as im:
        assert im.mode == "RGBA"
        assert im.size == canvas
        # On the line (mid-point): opaque lime.
        r, g, b, a = im.getpixel((5, 30))
        assert a > 0
        assert (r, g, b) == (0, 255, 0)
        # Far corner, nowhere near the stroke: fully transparent.
        assert im.getpixel((49, 0))[3] == 0


def test_render_guide_layer_wet_uses_cyan() -> None:
    png = _render_guide_layer("wet", (20, 20), segments=[[(2.0, 2.0), (2.0, 18.0)]], stroke_px=4)
    assert png is not None
    with Image.open(io.BytesIO(png)) as im:
        r, g, b, a = im.getpixel((2, 10))
        assert a > 0 and (r, g, b) == (0, 200, 255)


def test_render_guide_layer_rooms_draws_a_magenta_outline() -> None:
    png = _render_guide_layer(
        "rooms", (40, 40), rects=[((5.0, 5.0, 35.0, 35.0), "Kitchen")], labels=False,
    )
    assert png is not None
    with Image.open(io.BytesIO(png)) as im:
        # Top edge of the rectangle outline.
        r, g, b, a = im.getpixel((20, 5))
        assert a > 0 and (r, g, b) == (255, 0, 255)
        # Interior of the (unfilled) rectangle stays transparent.
        assert im.getpixel((20, 20))[3] == 0


def test_render_guide_layer_returns_none_when_nothing_to_draw() -> None:
    # docs/37 §4: a layer with no data isn't produced at all — no empty
    # transparent file, so the response's `paths` can signal "nothing here"
    # by simply omitting the key.
    assert _render_guide_layer("dry", (50, 50), segments=[]) is None
    assert _render_guide_layer("wet", (50, 50), segments=None) is None
    assert _render_guide_layer("rooms", (50, 50), rects=[]) is None
