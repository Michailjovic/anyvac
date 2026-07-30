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

from custom_components.anyvac.services import _floorplan_filename


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
