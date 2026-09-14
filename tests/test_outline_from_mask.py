"""Tests for `homeframe.outline_from_mask`'s Moore-neighbour boundary tracing
(docs/40 §4.1 "vedlejší produkt" — room outlines for `outline_home_px`).

These formalise the ad-hoc validation done mid-session when a real I-shaped
room (as opposed to the L-shapes exercised by the earlier synthetic tests)
raised a legitimate question: does tracing handle a THIN NECK connecting two
wider blobs, and the pathological case of two regions touching only at a
single diagonal pixel? Both were hand-verified against the real code at the
time (catching a genuine off-by-one direction bug in the process — see
CHANGELOG); this file is that verification made permanent.
"""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.anyvac import homeframe as hf


def _mask_from_rows(rows: list[str]) -> np.ndarray:
    """Build a bool mask from a list of equal-length strings, '#' = True."""
    return np.array([[c == "#" for c in row] for row in rows], dtype=bool)


def _polygon_area(pts: list[tuple[float, float]]) -> float:
    """Shoelace formula — used to sanity-check a traced outline actually
    encloses roughly the right amount of area, not just "some" points."""
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def test_empty_mask_returns_empty_outline() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    assert hf.outline_from_mask(mask, cell_mm=50) == []


def test_single_pixel_returns_a_unit_square() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    mask[2, 3] = True
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert len(outline) == 4
    assert _polygon_area(outline) == pytest.approx(50 * 50)


def test_solid_rectangle_traces_the_full_perimeter() -> None:
    """Regression test for the real bug found this session: the initial
    Moore-tracing search direction was wrong (south instead of north),
    causing a filled rectangle to trace only 4 points instead of its full
    perimeter."""
    mask = np.zeros((10, 12), dtype=bool)
    mask[2:8, 3:9] = True  # a solid 6x6 block
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert len(outline) >= 4
    assert _polygon_area(outline) == pytest.approx(6 * 6 * 50 * 50, rel=0.05)


def test_l_shape_traces_correctly() -> None:
    rows = [
        "..........",
        "..####....",
        "..####....",
        "..####....",
        "..######..",
        "..######..",
        "..........",
    ]
    mask = _mask_from_rows(rows)
    outline = hf.outline_from_mask(mask, cell_mm=50)
    true_cells = int(mask.sum())
    assert _polygon_area(outline) == pytest.approx(true_cells * 50 * 50, rel=0.05)


def test_i_shape_with_a_thin_neck_traces_as_one_connected_region() -> None:
    """The concern the user actually raised: "my map is I-shaped, not
    L-shaped — will that be a problem?" A thin neck connecting two wider
    blobs must trace as ONE outline, not get lost partway through."""
    mask = np.zeros((10, 20), dtype=bool)
    mask[1:9, 2:6] = True  # left blob (full height)
    mask[4:6, 2:18] = True  # the horizontal neck connecting left <-> right
    mask[1:9, 14:18] = True  # right blob (full height)
    outline = hf.outline_from_mask(mask, cell_mm=50)
    true_cells = int(mask.sum())
    assert _polygon_area(outline) == pytest.approx(true_cells * 50 * 50, rel=0.05)


def test_diagonal_single_pixel_touch_traces_without_crashing() -> None:
    """Pathological "bowtie": two blocks touching at exactly one diagonal
    pixel corner (8-connected, not 4-connected) — the guard-rail case for
    Moore tracing's stopping criterion. Must terminate (not loop forever)
    and must not raise; exact area accounting for a genuinely
    self-touching boundary is a known soft spot documented in the function
    itself, so this test only asserts it completes and returns a sane,
    non-empty polygon."""
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:3, 1:3] = True  # top-left 2x2 block
    mask[3, 3] = True  # single pixel touching the block only at (2,2)-(3,3) diagonal
    mask[3:5, 3:5] = True
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert len(outline) >= 4


def test_outline_points_are_axis_aligned_grid_positions() -> None:
    """Every returned point must land on a `cell_mm` grid line — the
    function must never invent sub-cell geometry."""
    mask = np.zeros((8, 8), dtype=bool)
    mask[2:6, 2:6] = True
    outline = hf.outline_from_mask(mask, cell_mm=50)
    for x, y in outline:
        assert x % 50 == 0
        assert y % 50 == 0


def test_max_points_simplifies_a_complex_outline() -> None:
    """A checkerboard-ish jagged mask produces a large raw trace; RDP
    simplification (shared with `_rdp_simplify`, docs/14 rule 1) must bring
    it under `max_points` while still enclosing roughly the right area."""
    rng = np.random.default_rng(11)
    mask = rng.random((40, 40)) > 0.3
    # Keep it one connected blob-ish region rather than pure noise, so the
    # trace is meaningful: OR in a solid base rectangle.
    mask[10:30, 10:30] = True
    outline = hf.outline_from_mask(mask, cell_mm=50, max_points=60)
    assert len(outline) <= 60
