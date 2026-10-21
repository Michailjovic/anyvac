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

from collections import deque

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
    and must not raise. Since integration 1.51.5 the two blocks stay
    separate components (closing does not join a diagonal-only touch), so
    the outline is one of them, with exact area."""
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:3, 1:3] = True  # top-left 2x2 block
    mask[3, 3] = True  # single pixel touching the block only at (2,2)-(3,3) diagonal
    mask[3:5, 3:5] = True
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert len(outline) >= 4
    # 4-connectivity: the two blocks are separate components of 4 cells
    # each; the tie goes to the first one in row-major order (top-left).
    assert _polygon_area(outline) == pytest.approx(4 * 50 * 50)
    assert max(x for x, _ in outline) == 150 and max(y for _, y in outline) == 150


def test_split_segment_traces_the_largest_component_not_the_topmost_island() -> None:
    """Field report (integration 1.51.4, S8 kitchen, segment 19): Roborock
    obstacle lines cut a 15 x 5-cell island off the top of the segment; the
    seed (topmost-then-leftmost pixel) fell into it and `outline_home_px` was
    a 0.75 x 0.25 m rectangle on the bbox's top edge. Here: a 40 x 40 room
    plus a 3 x 2 island above and to the left of it, one empty cell apart —
    the outline must be the big rectangle (1600 cells), not the island."""
    mask = np.zeros((50, 50), dtype=bool)
    mask[3:5, 1:4] = True  # island: rows 3-4, cols 1-3 (topmost pixel)
    mask[6:46, 5:45] = True  # main room: 40 x 40, one empty row below the island
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert _polygon_area(outline) == pytest.approx(1600 * 50 * 50)
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    assert (min(xs), min(ys), max(xs), max(ys)) == (5 * 50, 6 * 50, 45 * 50, 46 * 50)


def test_largest_component_returns_connected_mask_unchanged() -> None:
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:5, 1:3] = True
    assert hf.largest_component(mask) is mask


def test_largest_component_picks_the_biggest_part() -> None:
    mask = np.zeros((10, 10), dtype=bool)
    mask[0, 0:2] = True  # 2 cells, first in row-major order
    mask[3:6, 3:6] = True  # 9 cells
    mask[8, 8] = True  # 1 cell
    out = hf.largest_component(mask)
    assert int(out.sum()) == 9
    assert out[3:6, 3:6].all()


def _largest_component_reference(mask: np.ndarray) -> np.ndarray:
    """Obvious per-cell BFS — the oracle for the run-based union-find."""
    labels = np.zeros(mask.shape, dtype=int)
    n, best, best_size = 0, 0, 0
    for r, c in zip(*np.nonzero(mask)):
        if labels[r, c]:
            continue
        n += 1
        labels[r, c] = n
        queue, size = deque([(r, c)]), 0
        while queue:
            y, x = queue.popleft()
            size += 1
            for a, b in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= a < mask.shape[0] and 0 <= b < mask.shape[1] and mask[a, b] and not labels[a, b]:
                    labels[a, b] = n
                    queue.append((a, b))
        if size > best_size:
            best, best_size = n, size
    return mask if n <= 1 else labels == best


def test_largest_component_matches_a_per_cell_bfs_on_random_masks() -> None:
    rng = np.random.default_rng(7)
    for _ in range(300):
        h, w = rng.integers(1, 25, size=2)
        mask = rng.random((h, w)) < rng.uniform(0.2, 0.8)
        assert np.array_equal(hf.largest_component(mask), _largest_component_reference(mask))


def test_fragmented_segment_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """Three equal parts 3 cells apart (too far for `close_small_gaps`): the
    largest keeps 1/3 of the cells (< 50 %), so the outline misrepresents the
    room — logged once, not on every publish."""
    hf._OUTLINE_WARNED.clear()
    mask = np.zeros((10, 22), dtype=bool)
    mask[1:5, 1:5] = True
    mask[1:5, 8:12] = True
    mask[1:5, 15:19] = True
    with caplog.at_level("WARNING", logger=hf.__name__):
        hf.outline_from_mask(mask, cell_mm=50, label="S8 / Kitchen (segment 19)")
        hf.outline_from_mask(mask, cell_mm=50, label="S8 / Kitchen (segment 19)")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "S8 / Kitchen (segment 19)" in warnings[0].getMessage()
    assert "16 of 48" in warnings[0].getMessage()


# The real S8 MaxV Ultra kitchen (segment 19) from the 1.51.4 field report:
# its room cells (`(b & 7) == 7`) inside the 65 x 89-cell segment bbox whose
# top-left raw cell is (273, 88), run-length coded (alternating 0/1 runs,
# base 36, "." separated, starting with a 0-run). 16 parts; the topmost is
# the 15 x 5-cell strip 1.51.4 traced, the largest holds only 3088 of 3935.
_S8_KITCHEN_W, _S8_KITCHEN_H = 65, 89
_S8_KITCHEN_RLE = (
    "o.f.17.1.6.f.17.3.4.f.17.3.4.f.17.4.3.f.17.4.1p.4.3.f.1.b.1.b.j.6.1."
    "f.1.b.1.b.j.6.1.f.1.b.1.b.j.6.1.f.1.b.1.b.j.6.1.f.1.b.1.b.j.6.1.f.1."
    "b.1.b.j.6.1.f.1.b.1.b.j.6.1.f.1.b.1.b.q.f.1.b.1.b.j.6.1.f.1.b.1.b.j."
    "6.1.f.1.b.1.b.2b.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1."
    "g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b.1.1.g.1b."
    "1.1.g.1b.1.1.g.1b.i.1b.1.1.n.14.2.r3.4.1n.3.2.1.1n.1.1.4.1n.6.d.2.18."
    "l.18.9.9.3.18.l.18.l.18.l.18.l.18.l.18.l.18.l.18.l.18.l.18.l.18.l.18."
    "i.1.2.18.i.1b.i.1b.i.1b.i.1b.i.1b.i.1b.i.1b.i.1b.i.1b.2b.b.e.a.1.b.i."
    "b.e.a.1.b.i.b.e.a.1.b.m.7.e.a.1.b.m.7.e.a.1.b.n.1.j.a.1.b.q.3.e.a.1."
    "b.q.3.e.a.1.b.17.a.1.b.17.a.1.b.2"
)


def _decode_rle(rle: str, width: int, height: int) -> np.ndarray:
    bits: list[bool] = []
    value = False
    for run in rle.split("."):
        bits += [value] * int(run, 36)
        value = not value
    assert len(bits) == width * height
    return np.array(bits, dtype=bool).reshape(height, width)


def test_real_s8_kitchen_outline_covers_the_whole_room() -> None:
    mask = _decode_rle(_S8_KITCHEN_RLE, _S8_KITCHEN_W, _S8_KITCHEN_H)
    assert int(mask.sum()) == 3935
    assert int(hf.largest_component(mask).sum()) == 3088  # largest alone is not enough
    outline = hf.outline_from_mask(mask, cell_mm=50, max_points=10**6)
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    # The full segment extent (= the library bbox), not the 15 x 5 strip
    # 1.51.4 traced nor the 3088-cell middle part.
    assert (min(xs), min(ys), max(xs), max(ys)) == (0, 0, 65 * 50, 89 * 50)
    assert _polygon_area(outline) == pytest.approx(4129 * 50 * 50)


def test_obstacle_lines_inside_a_room_are_closed_into_one_outline() -> None:
    """The real S8 kitchen (segment 19, 1.51.4 field report): obstacle lines
    of 1 cell cut the room into 16 parts, a row of small squares along the
    top wall among them. Closing must restore the whole room: one outline
    with the full rectangle's extent and area, the lines filled."""
    mask = np.zeros((50, 50), dtype=bool)
    mask[5:45, 5:45] = True  # the room, 40 x 40
    mask[13, 5:45] = False  # full-width obstacle line cutting off the top strip
    for col in (15, 25, 35):  # the strip itself split into "squares"
        mask[5:13, col] = False
    mask[30:45, 20] = False  # a partial line (table edge) from the bottom wall
    outline = hf.outline_from_mask(mask, cell_mm=50)
    assert _polygon_area(outline) == pytest.approx(1600 * 50 * 50)
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    assert (min(xs), min(ys), max(xs), max(ys)) == (5 * 50, 5 * 50, 45 * 50, 45 * 50)


def test_close_small_gaps_bridges_two_cells_but_not_three() -> None:
    two = np.zeros((10, 20), dtype=bool)
    two[2:8, 2:8] = True
    two[2:8, 10:16] = True  # 2-cell gap (cols 8, 9)
    assert len(hf.close_small_gaps(two).nonzero()[0]) == 6 * 14
    three = np.zeros((10, 20), dtype=bool)
    three[2:8, 2:8] = True
    three[2:8, 11:17] = True  # 3-cell gap (cols 8-10)
    assert np.array_equal(hf.close_small_gaps(three), three)


def test_close_small_gaps_keeps_a_rectangle_and_the_grid_edge() -> None:
    """Closing must not grow a convex room, also when it touches the edge of
    the array (the 1-cell pad makes the edge plain background)."""
    mask = np.zeros((8, 8), dtype=bool)
    mask[0:5, 0:8] = True
    assert np.array_equal(hf.close_small_gaps(mask), mask)


def test_small_islands_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    hf._OUTLINE_WARNED.clear()
    mask = np.zeros((50, 50), dtype=bool)
    mask[3:5, 1:4] = True
    mask[6:46, 5:45] = True
    with caplog.at_level("WARNING", logger=hf.__name__):
        hf.outline_from_mask(mask, cell_mm=50)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


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
