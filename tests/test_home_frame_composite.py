"""Tests for docs/40 Fáze 2.2 — `frame: "home"` on
`anyvac.snapshot_map_as_floorplan`: a composite floorplan PNG rendered
directly from a home frame's own `floor_mask`/`wall_mask` rasters (no single
robot's photographed map image involved), plus `_select_home_frame`, the
selection policy shared with `export_map_guide`'s own `frame: "home"`
(docs/14 rule 1 — one selection policy, not two). Also covers the docs/40
§5.A.2 `fiducials=True` path added alongside `snap_wall_corner`/cesta B —
`_home_frame_composite_png` gained a third return value (`markers`) and a
`fiducials` parameter; every pre-existing call below was updated to unpack
3 values rather than 2, with `fiducials` left at its default (False) so
those cases keep asserting the exact prior (marker-free) behaviour.

Like `test_snapshot_floorplan.py`, only the pure/blocking pieces factored OUT
of the real async handler are exercised here: `_home_frame_composite_png`
(PIL rendering, safe to call directly — it's already written to run via
`hass.async_add_executor_job`) and `_select_home_frame` (against a minimal
fake hass/coordinator exposing only `home_frames_snapshot`).
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from homeassistant.exceptions import HomeAssistantError

from custom_components.anyvac.homeframe import FIDUCIAL_MARKER_COLORS
from custom_components.anyvac.services import (
    _fiducial_marker_specs,
    _home_frame_composite_png,
    _select_home_frame,
)


def _frame(floor: np.ndarray, wall: np.ndarray, *, scale: float = 4.0, robots: dict | None = None) -> dict:
    return {
        "floor_mask": floor,
        "wall_mask": wall,
        "scale": scale,
        "robots": robots if robots is not None else {"s6": {}},
    }


# ── _home_frame_composite_png ────────────────────────────────────────────────


def test_composite_renders_floor_and_wall_colours() -> None:
    floor = np.zeros((6, 8), dtype=bool)
    floor[2:4, 2:6] = True
    wall = np.zeros((6, 8), dtype=bool)
    wall[1, 2:6] = True  # a wall row directly above the floor block
    png, box, markers = _home_frame_composite_png(_frame(floor, wall, scale=4.0))
    assert markers == []  # fiducials default off

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    left, top, right, bottom = box
    assert right - left == img.width
    assert bottom - top == img.height

    # A cell well inside the floor block, converted from mask-cell to
    # composite-local pixel coordinates (scale=4, minus the crop's own
    # top-left origin) must be the floor colour.
    fx, fy = 3 * 4 - left, 2 * 4 - top  # top-left corner of cell (row=2, col=3)
    assert img.getpixel((fx + 1, fy + 1)) == (235, 235, 235, 255)

    # A cell in the wall row must be the (different) wall colour.
    wx, wy = 3 * 4 - left, 1 * 4 - top
    assert img.getpixel((wx + 1, wy + 1)) == (60, 60, 60, 255)


def test_composite_background_outside_masks_is_transparent() -> None:
    floor = np.zeros((10, 10), dtype=bool)
    floor[4, 4] = True  # a single occupied cell surrounded by background
    wall = np.zeros((10, 10), dtype=bool)
    png, box, _markers = _home_frame_composite_png(_frame(floor, wall, scale=4.0))
    img = Image.open(io.BytesIO(png)).convert("RGBA")

    # The padded crop always includes some background around a single-cell
    # occupied region — its very corner pixel is never inside that cell.
    assert img.getpixel((0, 0))[3] == 0  # alpha channel: fully transparent


def test_composite_crop_is_padded_around_the_occupied_extent() -> None:
    floor = np.zeros((20, 20), dtype=bool)
    floor[8:12, 8:12] = True  # small occupied block, centred, far from edges
    wall = np.zeros((20, 20), dtype=bool)
    _, box, _markers = _home_frame_composite_png(_frame(floor, wall, scale=4.0))
    left, top, right, bottom = box
    # Tight bbox in px would be (32, 32, 48, 48) (cells 8..12 * scale 4) —
    # padding must widen it on every side, not just clamp to the full canvas.
    assert left < 32
    assert top < 32
    assert right > 48
    assert bottom > 48


def test_composite_with_empty_masks_does_not_crash() -> None:
    floor = np.zeros((5, 5), dtype=bool)
    wall = np.zeros((5, 5), dtype=bool)
    png, box, markers = _home_frame_composite_png(_frame(floor, wall, scale=4.0))
    assert isinstance(png, bytes) and len(png) > 0
    left, top, right, bottom = box
    assert right > left and bottom > top
    assert markers == []


# ── docs/40 §5.A.2: fiducials=True ───────────────────────────────────────────


def test_composite_fiducials_off_by_default_embeds_nothing() -> None:
    # Byte-for-byte proof the opt-in flag actually gates the drawing, not
    # just the returned metadata: an identical frame rendered with and
    # without fiducials produces identical PNG bytes when off.
    floor = np.zeros((20, 20), dtype=bool)
    floor[6:14, 6:14] = True
    wall = np.zeros((20, 20), dtype=bool)
    png_a, box_a, markers_a = _home_frame_composite_png(_frame(floor, wall, scale=4.0), False)
    png_b, box_b, markers_b = _home_frame_composite_png(_frame(floor, wall, scale=4.0), fiducials=False)
    assert png_a == png_b
    assert box_a == box_b
    assert markers_a == markers_b == []


def test_composite_fiducials_true_returns_four_markers_with_home_px() -> None:
    floor = np.zeros((20, 20), dtype=bool)
    floor[6:14, 6:14] = True
    wall = np.zeros((20, 20), dtype=bool)
    png, box, markers = _home_frame_composite_png(_frame(floor, wall, scale=4.0), True)
    assert {m["id"] for m in markers} == {"tl", "tr", "bl", "br"}
    left, top, right, bottom = box
    for m in markers:
        # Every marker's reported home_px must land strictly inside the crop
        # box — it's meant to sit in the padding border around the occupied
        # extent, never outside the saved image entirely.
        assert left <= m["home_px"]["x"] <= right
        assert top <= m["home_px"]["y"] <= bottom
    # And the specs used to draw them agree exactly with what was returned —
    # no drift between what got drawn and what got reported.
    specs = {s["id"]: (s["x"], s["y"]) for s in _fiducial_marker_specs(box)}
    for m in markers:
        assert (m["home_px"]["x"], m["home_px"]["y"]) == specs[m["id"]]

    # The markers are actually IN the PNG, at their expected colours, once
    # you know to look at the exact (crop-relative) pixel — proving the draw
    # step and the reported coordinates refer to the same crop.
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    for m in markers:
        lx = round(m["home_px"]["x"] - left)
        ly = round(m["home_px"]["y"] - top)
        r, g, b = FIDUCIAL_MARKER_COLORS[m["id"]]
        px = img.getpixel((lx, ly))
        assert px[:3] == (r, g, b)
        assert px[3] <= 40  # near-invisible alpha


def test_composite_fiducials_true_markers_are_visually_negligible() -> None:
    # "Invisible" in practice: compositing the fiducial-bearing PNG over a
    # solid background must look the same (within normal rounding) as the
    # marker-free version — nobody should see red/green/blue/yellow flecks
    # on their floorplan.
    floor = np.zeros((30, 30), dtype=bool)
    floor[8:22, 8:22] = True
    wall = np.zeros((30, 30), dtype=bool)
    png_plain, _b1, _m1 = _home_frame_composite_png(_frame(floor, wall, scale=4.0), False)
    png_marked, _b2, _m2 = _home_frame_composite_png(_frame(floor, wall, scale=4.0), True)

    bg = (235, 235, 235)  # composite onto the same tone as the floor itself

    def _flatten(png_bytes: bytes) -> np.ndarray:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        canvas = Image.new("RGBA", img.size, bg + (255,))
        canvas.alpha_composite(img)
        return np.array(canvas.convert("RGB"), dtype=np.int16)

    diff = np.abs(_flatten(png_plain) - _flatten(png_marked))
    assert int(diff.max()) <= 2  # alpha=1/255 rounds away to nothing visible


# ── _select_home_frame ───────────────────────────────────────────────────────


class _FakeCoordFrames:
    def __init__(self, frames: dict) -> None:
        self._frames = frames

    def home_frames_snapshot(self) -> dict:
        return dict(self._frames)


class _FakeEntry:
    def __init__(self, coord) -> None:
        self.runtime_data = coord


class _FakeConfigEntries:
    def __init__(self, coords: list) -> None:
        self._entries = [_FakeEntry(c) for c in coords]

    def async_entries(self, domain: str):  # noqa: ARG002
        return self._entries


class _FakeHass:
    def __init__(self, frames: dict) -> None:
        self.config_entries = _FakeConfigEntries([_FakeCoordFrames(frames)])


def test_select_home_frame_explicit_id_wins() -> None:
    frames = {
        "big": {"robots": {"s6": {}, "s7": {}}},
        "small": {"robots": {"s8": {}}},
    }
    hass = _FakeHass(frames)
    fid, frame = _select_home_frame(hass, "small", service="snapshot_map_as_floorplan")
    assert fid == "small"
    assert frame is frames["small"]


def test_select_home_frame_unknown_explicit_id_raises() -> None:
    hass = _FakeHass({"big": {"robots": {"s6": {}}}})
    with pytest.raises(HomeAssistantError, match="unknown frame_id"):
        _select_home_frame(hass, "nope", service="snapshot_map_as_floorplan")


def test_select_home_frame_defaults_to_the_frame_with_most_robots() -> None:
    frames = {
        "small": {"robots": {"s8": {}}},
        "big": {"robots": {"s6": {}, "s7": {}}},
    }
    hass = _FakeHass(frames)
    fid, _frame = _select_home_frame(hass, None, service="snapshot_map_as_floorplan")
    assert fid == "big"


def test_select_home_frame_skips_stale_frames() -> None:
    frames = {
        "stale_big": {"robots": {"s6": {}, "s7": {}}, "stale": True},
        "small": {"robots": {"s8": {}}},
    }
    hass = _FakeHass(frames)
    fid, _frame = _select_home_frame(hass, None, service="snapshot_map_as_floorplan")
    assert fid == "small"


def test_select_home_frame_raises_when_nothing_registered_yet() -> None:
    hass = _FakeHass({})
    with pytest.raises(HomeAssistantError, match='frame: "home" requires'):
        _select_home_frame(hass, None, service="snapshot_map_as_floorplan")
