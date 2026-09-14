"""Tests for docs/40 §5.A.2 — fiducial-marker DETECTION: the other half of
the feature `test_home_frame_composite.py`'s `fiducials=True` tests cover
the EMBEDDING side of (`_embed_fiducial_markers`/`_fiducial_marker_specs`).

Covers, bottom-up:

- `homeframe.find_fiducial_markers` — the pure, vectorised colour/alpha scan
  over a raw RGBA array. Hand-derived: every test builds an array where the
  exact matching pixels (and therefore the exact expected centroid) are
  known by construction, never checked against the function's own output.
- `services._resolve_local_www_path` — the `/local/...` URL -> filesystem
  path shuttle `detect_floorplan_fiducials` uses to read back a file the
  user may have edited externally.
- `services._detect_fiducials` — the pure function behind
  `anyvac.detect_floorplan_fiducials`: opens an actual PNG (round-tripped
  through PIL, exactly like the real service will receive one), finds the
  markers, and pairs them against a supplied `known` list into
  `home_anchors` pairs — the SAME `{home_px, floor_pct}` shape cesta B's
  `image_base.home_anchors` already stores (docs/14 rule 1).
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from custom_components.anyvac.homeframe import (
    FIDUCIAL_ALPHA_MAX,
    FIDUCIAL_MARKER_COLORS,
    find_fiducial_markers,
)
from custom_components.anyvac.services import _detect_fiducials, _resolve_local_www_path


# ── find_fiducial_markers ─────────────────────────────────────────────────────


def _blank_rgba(w: int, h: int) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 3] = 255  # fully opaque background, like a real photo
    return arr


def test_find_fiducial_markers_all_none_on_blank_image() -> None:
    arr = _blank_rgba(20, 20)
    found = find_fiducial_markers(arr)
    assert found == {"tl": None, "tr": None, "bl": None, "br": None}


def test_find_fiducial_markers_locates_a_single_marker_by_centroid() -> None:
    arr = _blank_rgba(20, 20)
    r, g, b = FIDUCIAL_MARKER_COLORS["tl"]
    # A solid 3x3 block at rows 4..6, cols 4..6 (inclusive) — hand-computed
    # centroid: mean index 5 on both axes, +0.5 landing on pixel centres.
    arr[4:7, 4:7] = (r, g, b, 1)
    found = find_fiducial_markers(arr)
    assert found["tl"] == pytest.approx((5.5, 5.5))
    assert found["tr"] is None
    assert found["bl"] is None
    assert found["br"] is None


def test_find_fiducial_markers_distinguishes_all_four_by_colour_not_position() -> None:
    arr = _blank_rgba(40, 40)
    # Deliberately scattered, NOT in their "natural" corners — proves colour
    # identity alone drives the match, exactly the property that lets a
    # rotated/mirrored file still resolve correctly (docs/40 §5.A.2).
    placements = {
        "tl": (30, 2),  # top-right-ish physically, but coloured "tl"
        "tr": (2, 30),
        "bl": (30, 30),
        "br": (2, 2),
    }
    for mid, (x, y) in placements.items():
        r, g, b = FIDUCIAL_MARKER_COLORS[mid]
        arr[y : y + 1, x : x + 1] = (r, g, b, 1)  # single pixel, centroid = (x+0.5, y+0.5)
    found = find_fiducial_markers(arr)
    for mid, (x, y) in placements.items():
        assert found[mid] == pytest.approx((x + 0.5, y + 0.5))


def test_find_fiducial_markers_rejects_high_alpha_even_with_exact_colour() -> None:
    arr = _blank_rgba(10, 10)
    r, g, b = FIDUCIAL_MARKER_COLORS["tl"]
    arr[4:6, 4:6] = (r, g, b, FIDUCIAL_ALPHA_MAX + 20)  # opaque-ish, not a marker
    found = find_fiducial_markers(arr)
    assert found["tl"] is None


def test_find_fiducial_markers_tolerates_slight_colour_drift_from_resize() -> None:
    arr = _blank_rgba(10, 10)
    r, g, b = FIDUCIAL_MARKER_COLORS["br"]
    # Nudge every channel by 10 (well inside FIDUCIAL_COLOR_TOLERANCE=40,
    # squared-euclidean distance = 3*10**2 = 300 << 40**2=1600) — simulates
    # what a bit of resize interpolation blur would do to an interior pixel.
    drifted = (min(255, r + 10), min(255, g - 10) if g >= 10 else g + 10, min(255, b + 10))
    arr[3:5, 3:5] = (*drifted, 1)  # a 2x2 block (indices 3,4) -> mean 3.5, +0.5 = 4.0
    found = find_fiducial_markers(arr)
    assert found["br"] == pytest.approx((4.0, 4.0))


def test_find_fiducial_markers_rejects_colour_far_outside_tolerance() -> None:
    arr = _blank_rgba(10, 10)
    arr[3:5, 3:5] = (128, 128, 128, 1)  # grey — nowhere near any of the 4 hues
    found = find_fiducial_markers(arr)
    assert all(v is None for v in found.values())


def test_find_fiducial_markers_returns_all_none_without_alpha_channel() -> None:
    rgb_only = np.zeros((10, 10, 3), dtype=np.uint8)  # no alpha dimension at all
    found = find_fiducial_markers(rgb_only)
    assert found == {"tl": None, "tr": None, "bl": None, "br": None}


# ── _resolve_local_www_path ───────────────────────────────────────────────────


class _FakeConfig:
    def path(self, *parts: str) -> str:
        return "/config/" + "/".join(parts)


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()


def test_resolve_local_www_path_strips_local_prefix_and_cache_bust_query() -> None:
    hass = _FakeHass()
    got = _resolve_local_www_path(hass, "/local/anyvac/home_frame_calib.png?t=1699999999")
    assert got == "/config/www/anyvac/home_frame_calib.png"


def test_resolve_local_www_path_handles_nested_subfolders() -> None:
    hass = _FakeHass()
    got = _resolve_local_www_path(hass, "/local/anyvac/debug/foo.png")
    assert got == "/config/www/anyvac/debug/foo.png"


def test_resolve_local_www_path_rejects_non_local_urls() -> None:
    hass = _FakeHass()
    with pytest.raises(ValueError, match="expected a '/local/"):
        _resolve_local_www_path(hass, "https://example.com/floorplan.png")


def test_resolve_local_www_path_rejects_bare_filesystem_paths() -> None:
    hass = _FakeHass()
    with pytest.raises(ValueError, match="expected a '/local/"):
        _resolve_local_www_path(hass, "/config/www/anyvac/foo.png")


# ── _detect_fiducials ──────────────────────────────────────────────────────────
# All 4 marker rectangles below are drawn INCLUSIVE via PIL's draw.rectangle,
# so a box [x0, y0, x1, y1] fills every integer pixel x0..x1 and y0..y1.
# find_fiducial_markers' centroid = mean(matching index) + 0.5, and the mean
# of consecutive integers x0..x1 is (x0+x1)/2 — so each centroid below is
# hand-computable directly from the drawn box, not read off any code's own
# output. Canvas is 100x50 so every floor_pct works out to a clean number.

_KNOWN = [
    {"id": "tl", "home_px": {"x": 100.0, "y": 200.0}},
    {"id": "tr", "home_px": {"x": 500.0, "y": 200.0}},
    {"id": "bl", "home_px": {"x": 100.0, "y": 800.0}},
    {"id": "br", "home_px": {"x": 500.0, "y": 800.0}},
]


def _make_test_png(*, include: set[str]) -> bytes:
    img = Image.new("RGBA", (100, 50), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    # box -> hand-computed centroid -> floor_pct (canvas 100x50):
    #   tl: [5,5,9,9]   -> (7.5, 7.5)   -> (7.5, 15.0)
    #   tr: [85,5,89,9] -> (87.5, 7.5)  -> (87.5, 15.0)
    #   bl: [5,35,9,39] -> (7.5, 37.5)  -> (7.5, 75.0)
    #   br: [85,35,89,39] -> (87.5, 37.5) -> (87.5, 75.0)
    boxes = {
        "tl": [5, 5, 9, 9],
        "tr": [85, 5, 89, 9],
        "bl": [5, 35, 9, 39],
        "br": [85, 35, 89, 39],
    }
    for mid in include:
        r, g, b = FIDUCIAL_MARKER_COLORS[mid]
        draw.rectangle(boxes[mid], fill=(r, g, b, 1))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_detect_fiducials_all_four_found_produces_matching_home_anchors() -> None:
    png = _make_test_png(include={"tl", "tr", "bl", "br"})
    result = _detect_fiducials(png, _KNOWN)
    assert result["found"] == 4
    assert result["missing"] == []
    assert result["image_width"] == 100
    assert result["image_height"] == 50

    anchors_by_home_px = {
        (a["home_px"]["x"], a["home_px"]["y"]): a["floor_pct"] for a in result["home_anchors"]
    }
    expected = {
        (100.0, 200.0): (7.5, 15.0),
        (500.0, 200.0): (87.5, 15.0),
        (100.0, 800.0): (7.5, 75.0),
        (500.0, 800.0): (87.5, 75.0),
    }
    assert set(anchors_by_home_px.keys()) == set(expected.keys())
    for home_px, expected_pct in expected.items():
        got = anchors_by_home_px[home_px]
        assert (got["x"], got["y"]) == pytest.approx(expected_pct)


def test_detect_fiducials_reports_missing_markers_without_failing() -> None:
    png = _make_test_png(include={"tl", "tr"})  # bl/br never drawn
    result = _detect_fiducials(png, _KNOWN)
    assert result["found"] == 2
    assert sorted(result["missing"]) == ["bl", "br"]
    assert len(result["home_anchors"]) == 2


def test_detect_fiducials_none_found_returns_empty_not_an_error() -> None:
    # The pure function itself never raises for zero matches — that's the
    # service HANDLER's job (docs/14: keep the pure core simple/testable,
    # push HA-specific error surfacing to the thin closure around it).
    png = _make_test_png(include=set())
    result = _detect_fiducials(png, _KNOWN)
    assert result["found"] == 0
    assert sorted(result["missing"]) == ["bl", "br", "tl", "tr"]
    assert result["home_anchors"] == []
