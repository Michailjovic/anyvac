"""Tests for the pure filename-generation logic behind
``anyvac.snapshot_map_as_floorplan`` (2026-07-30).

Field context: merged mode's per-vacuum auto-seat fit is hard-disabled without
a shared floorplan image (`_editorSeat`/`_effectiveSeat` in the card both bail
to manual sliders when `image_base.src` is unset) — getting a usable floorplan
photo today meant manually saving a map image out of HA and re-uploading it
into `config/www/`. This service lets the card do that in one click instead;
`_floorplan_filename` is the only part of the handler that doesn't need a real
Home Assistant instance (network image fetch + file I/O) to exercise, so it's
factored out and tested directly here.
"""

from __future__ import annotations

import io

from PIL import Image

from custom_components.anyvac.services import (
    _crop_image_to_bbox,
    _floorplan_filename,
    _padded_crop_box,
    _room_union_bbox_px,
)


def test_known_content_types_map_to_expected_extension() -> None:
    assert _floorplan_filename("S6", "image/png").endswith(".png")
    assert _floorplan_filename("S6", "image/jpeg").endswith(".jpg")
    assert _floorplan_filename("S6", "image/jpg").endswith(".jpg")
    assert _floorplan_filename("S6", "image/webp").endswith(".webp")
    assert _floorplan_filename("S6", "image/gif").endswith(".gif")


def test_unknown_or_missing_content_type_falls_back_to_png() -> None:
    assert _floorplan_filename("S6", "application/octet-stream").endswith(".png")
    assert _floorplan_filename("S6", None).endswith(".png")
    assert _floorplan_filename("S6", "").endswith(".png")


def test_content_type_matching_is_case_insensitive() -> None:
    assert _floorplan_filename("S6", "IMAGE/PNG").endswith(".png")
    assert _floorplan_filename("S6", "Image/Jpeg").endswith(".jpg")


def test_name_is_slugified() -> None:
    assert _floorplan_filename("S6 MaxV Ultra!", "image/png") == "anyvac_floorplan_s6_maxv_ultra.png"
    assert _floorplan_filename("s6_kitchen_map_0", "image/png") == "anyvac_floorplan_s6_kitchen_map_0.png"


def test_empty_or_fully_stripped_name_falls_back_to_vacuum() -> None:
    assert _floorplan_filename("", "image/png") == "anyvac_floorplan_vacuum.png"
    assert _floorplan_filename("!!!", "image/png") == "anyvac_floorplan_vacuum.png"


def test_filename_is_stable_for_the_same_inputs() -> None:
    # Re-snapshotting the same vacuum must produce the same filename each time
    # (so image_base.src, set once, keeps pointing at the right file — only the
    # cache-busting query string added by the caller changes between runs).
    assert _floorplan_filename("S8 MaxV Ultra", "image/png") == _floorplan_filename(
        "S8 MaxV Ultra", "image/png"
    )


# ── Crop-to-content (docs/30 §4a second field follow-up) ──────────────────────
# Field report: a snapshotted floorplan came out "4:3 and doesn't crop the
# empty space" — Roborock's map canvas is much larger than the actually
# explored area. `_room_union_bbox_px`/`_padded_crop_box`/`_crop_image_to_bbox`
# trim the snapshot to the union of the vacuum's own known room bounding
# boxes instead of guessing from pixel colour.


def test_union_bbox_covers_all_rooms() -> None:
    rooms = [
        {"bbox_px": {"x0": 100, "y0": 200, "x1": 300, "y1": 400}},
        {"bbox_px": {"x0": 50, "y0": 500, "x1": 250, "y1": 600}},
    ]
    assert _room_union_bbox_px(rooms) == (50, 200, 300, 600)


def test_union_bbox_skips_rooms_without_a_usable_bbox() -> None:
    rooms = [
        {"bbox_px": None},
        {"bbox_px": {"x0": 10, "y0": 20, "x1": 30, "y1": 40}},
        {"name": "no bbox key at all"},
        {"bbox_px": {"x0": None, "y0": 20, "x1": 30, "y1": 40}},  # partial -> skipped
    ]
    assert _room_union_bbox_px(rooms) == (10, 20, 30, 40)


def test_union_bbox_none_when_nothing_usable() -> None:
    assert _room_union_bbox_px([]) is None
    assert _room_union_bbox_px([{"bbox_px": None}, {"name": "x"}]) is None


def test_padded_crop_box_adds_margin_and_clamps_to_image() -> None:
    # A tight bbox near the image edge should pad outward but never past 0
    # or the image dimensions.
    box = _padded_crop_box((0, 0, 100, 50), img_w=1000, img_h=1000)
    assert box[0] == 0 and box[1] == 0  # clamped, can't pad below 0
    assert box[2] > 100 and box[3] > 50  # padded outward on the far side

    # A bbox comfortably inside the image pads symmetrically on both sides.
    box2 = _padded_crop_box((200, 200, 400, 400), img_w=1000, img_h=1000)
    assert box2[0] < 200 and box2[1] < 200
    assert box2[2] > 400 and box2[3] > 400


def _make_test_image(size: tuple[int, int] = (800, 600)) -> bytes:
    im = Image.new("RGB", size, color=(0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_crop_image_to_bbox_shrinks_to_the_padded_room_area() -> None:
    content = _make_test_image((800, 600))
    bbox = (100.0, 100.0, 300.0, 250.0)
    new_content, content_type, box = _crop_image_to_bbox(content, bbox)
    assert content_type == "image/png"
    expected_box = _padded_crop_box(bbox, 800, 600)
    assert box == expected_box
    with Image.open(io.BytesIO(new_content)) as cropped:
        expected_w = expected_box[2] - expected_box[0]
        expected_h = expected_box[3] - expected_box[1]
        assert cropped.size == (expected_w, expected_h)
        # Cropped image should be meaningfully smaller than the original —
        # this is the whole point (trimming the unexplored padding).
        assert cropped.size[0] < 800 and cropped.size[1] < 600


def test_crop_image_to_bbox_returns_box_in_the_same_px_space_as_bbox_px() -> None:
    # docs/30 §8: the returned box is what the card uses to re-normalise
    # bbox_px onto the cropped image (placeRoomInCrop) — it must be usable
    # directly against the SAME room bboxes that produced it, with no unit
    # conversion. A room bbox fully inside the crop should map to a sane
    # 0-100% position once shifted by the returned box's own origin.
    content = _make_test_image((800, 600))
    bbox = (100.0, 100.0, 300.0, 250.0)
    _new_content, _content_type, box = _crop_image_to_bbox(content, bbox)
    left, top, right, bottom = box
    room_cx = (bbox[0] + bbox[2]) / 2
    room_cy = (bbox[1] + bbox[3]) / 2
    assert left <= room_cx <= right
    assert top <= room_cy <= bottom
