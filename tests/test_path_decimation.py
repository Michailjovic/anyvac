"""Tests for path trace simplification (`_decimate`/`_rdp_simplify` in
coordinator.py).

Root cause fixed here (found from live field data, 2026-07-22): the old
`_decimate` used naive every-Nth-point stride sampling recomputed from the
FULL accumulated trajectory every poll. A real full-apartment dry session had
~3000 raw points against the old `PATH_MAX_POINTS = 400` cap — an ~8x uniform
thinning that visibly butchered turns (where points naturally cluster) while
barely touching long straight sweeps, and got progressively worse over the
course of a session since the same fixed budget was reapplied to an
ever-growing point count. Confirmed live: S6 3015 -> 377 points, S7 MaxV
2886 -> 361.

The fix: `_rdp_simplify` (Ramer-Douglas-Peucker) drops a point only if it's
within `_RDP_EPSILON_MM` of the straight line between its kept neighbours —
a straight run collapses to its two endpoints almost for free, while a real
corner survives because removing it would visibly bend the line. `_decimate`
still enforces `max_points` as a hard safety cap (now 2000, up from 400) via
a uniform-stride fallback applied AFTER simplification, for pathological
(very long) sessions — this should essentially never trigger for a normal
home.
"""

from __future__ import annotations

import custom_components.anyvac.coordinator as coordinator_mod

_decimate = coordinator_mod._decimate
_rdp_simplify = coordinator_mod._rdp_simplify
_decimate_segments = coordinator_mod._decimate_segments


def _line(n: int, x0: float = 0.0, y0: float = 0.0, dx: float = 10.0, dy: float = 0.0) -> list[dict[str, float]]:
    return [{"x": x0 + i * dx, "y": y0 + i * dy} for i in range(n)]


def test_straight_run_collapses_to_endpoints() -> None:
    """A perfectly straight run carries no shape information beyond its two
    ends -- RDP should drop everything in between regardless of point count."""
    pts = _line(500)
    out = _rdp_simplify(pts, epsilon=20.0)
    assert out == [pts[0], pts[-1]]


def test_sharp_corner_survives_simplification() -> None:
    """A real 90-degree turn (like a vacuum finishing a row and starting the
    next) must NOT be smoothed away, even though the epsilon is generous
    relative to typical point spacing."""
    # Two straight legs of 100 densely-spaced points each, meeting at a sharp
    # corner at (1000, 0).
    leg1 = _line(100, x0=0.0, y0=0.0, dx=10.0, dy=0.0)          # (0,0) -> (990,0)
    leg2 = [{"x": 1000.0, "y": i * 10.0} for i in range(100)]    # (1000,0) -> (1000,990)
    pts = leg1 + leg2
    out = _rdp_simplify(pts, epsilon=20.0)
    # The corner point (or one immediately adjacent) must be kept -- a result
    # that skipped straight past the turn would only have 2 points (the very
    # first and very last), losing the corner entirely.
    assert len(out) >= 3
    corner_kept = any(abs(p["x"] - 1000.0) < 15.0 and abs(p["y"]) < 15.0 for p in out)
    assert corner_kept


def test_decimate_respects_hard_cap_even_when_shape_is_complex() -> None:
    """A pathological trajectory (every point a sharp corner -- RDP's near-
    worst case, and the case where simplification alone won't shrink much)
    must still never exceed max_points -- the uniform-stride fallback is the
    safety net for exactly this case."""
    # Zigzag: alternating up/down by a large amount every step, so almost
    # every point is "far" from the straight line between its neighbours.
    pts = [{"x": i * 5.0, "y": 1000.0 if i % 2 == 0 else 0.0} for i in range(3000)]
    out = _decimate(pts, max_points=400)
    assert len(out) <= 400


def test_decimate_under_budget_is_shape_preserving_not_naive_stride() -> None:
    """The regression this whole fix targets: with today's higher budget, a
    realistic full-session point count should simplify via RDP (not fall back
    to the naive stride path) and land well under the cap with the corner
    intact."""
    leg1 = _line(1500, x0=0.0, y0=0.0, dx=2.0, dy=0.0)           # long straight run
    leg2 = [{"x": 3000.0, "y": i * 2.0} for i in range(1500)]     # long straight run
    pts = leg1 + leg2  # 3000 points total, matching the live field data scale
    out = _decimate(pts, max_points=2000)
    assert len(out) < 100  # two straight legs -> should collapse to a handful of points
    corner_kept = any(abs(p["x"] - 3000.0) < 15.0 and abs(p["y"]) < 15.0 for p in out)
    assert corner_kept


def test_decimate_never_grows_the_input() -> None:
    pts = _line(50)
    assert _decimate(pts, max_points=2000) == pts  # already under budget -> untouched


def test_decimate_segments_still_preserves_segment_boundaries() -> None:
    """Unchanged contract (docs/14 §3.9): segments are simplified
    independently and never merged/reordered, so an excluded transit/mop-wash
    gap between them is still never bridged by a straight line."""
    seg_a = _line(600, x0=0.0, y0=0.0, dx=5.0, dy=0.0)
    seg_b = _line(600, x0=10000.0, y0=10000.0, dx=5.0, dy=0.0)  # far away, different "room"
    out = _decimate_segments([seg_a, seg_b], max_points=400)
    assert len(out) == 2
    assert all(len(s) <= 400 for s in out)
    # No point from segment A should ever appear in segment B's output or vice
    # versa -- confirms independence (a merge bug would show up as a point
    # near the wrong segment's origin).
    assert all(p["x"] < 5000.0 for p in out[0])
    assert all(p["x"] > 5000.0 for p in out[1])
