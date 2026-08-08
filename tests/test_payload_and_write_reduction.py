"""Tests for the 1.1.0 payload / disk-write reductions (docs/34 O2-O4).

Three independent changes, all measured before being made:

* `_decimate_segments` memoises closed segments — it used to re-run RDP over the
  WHOLE accumulated trace every 30 s poll, so cost grew with session length
  rather than with new data.
* `_paths_for_save` writes simplified points — the store is a full-file rewrite
  and held the raw trajectory (~378 KiB/write for three vacuums at 3000 points,
  ~66 MiB over a three-hour job).
* The legacy mm path arrays are gone from the sensor payload unless the new
  config-entry option asks for them (~224 KiB per vacuum per poll that the card
  has not read since Fáze 3 of the canon).
"""

from __future__ import annotations

import json

import custom_components.anyvac.coordinator as coordinator_mod

_decimate_segments = coordinator_mod._decimate_segments
_rdp_simplify = coordinator_mod._rdp_simplify
_RDP_EPSILON_MM = coordinator_mod._RDP_EPSILON_MM


def _zigzag(n: int, x0: float = 0.0) -> list[dict[str, float]]:
    """A path RDP cannot collapse — every point is a genuine corner."""
    return [{"x": x0 + i * 30.0, "y": 0.0 if i % 2 == 0 else 300.0} for i in range(n)]


def test_decimation_cache_returns_identical_output_to_no_cache() -> None:
    """The memo must be a pure speed-up: same input, same output, cache or not."""
    segments = [_zigzag(1200), _zigzag(1500, 10_000.0)]
    plain = _decimate_segments(segments, 2000)
    cache: dict = {}
    cached_first = _decimate_segments(segments, 2000, cache)
    cached_again = _decimate_segments(segments, 2000, cache)
    assert cached_first == plain
    assert cached_again == plain


def test_decimation_cache_recomputes_when_a_segment_grows() -> None:
    """A growing (still-open) last segment must not serve a stale memo — and the
    closed segment before it must not be recomputed needlessly either."""
    closed = _zigzag(1200)
    cache: dict = {}
    first = _decimate_segments([closed, _zigzag(400)], 2000, cache)
    grown = _zigzag(900)
    second = _decimate_segments([closed, grown], 2000, cache)

    assert second[1] != first[1], "the grown segment must be recomputed"
    # Same output as a cold run over the same input.
    assert second == _decimate_segments([closed, grown], 2000)


def test_decimation_cache_actually_skips_closed_segments(monkeypatch) -> None:
    """The three tests above assert the memo is OUTPUT-neutral, which by design
    they also pass with the memo disabled — so on their own they'd let the
    optimisation silently regress to a no-op. This one pins the point of it:
    count real `_decimate` invocations and require that a second pass over an
    unchanged trace does no work at all, and that appending to the open segment
    recomputes ONLY that segment."""
    # Count `_simplify` (the RDP step), which is the expensive half the memo
    # exists to skip — `_cap` is an O(n) stride that always runs.
    calls: list[int] = []
    real = coordinator_mod._simplify

    def _counting(points):
        calls.append(len(points))
        return real(points)

    monkeypatch.setattr(coordinator_mod, "_simplify", _counting)

    # Total must exceed max_points, otherwise `_decimate_segments` short-circuits
    # and never calls `_decimate` at all (which is itself the right behaviour —
    # nothing to trim — just not what this test is about).
    closed_a, closed_b = _zigzag(1200), _zigzag(1200, 50_000.0)
    cache: dict = {}

    _decimate_segments([closed_a, closed_b, _zigzag(400)], 2000, cache)
    assert len(calls) == 3, "cold run must process every segment"

    calls.clear()
    _decimate_segments([closed_a, closed_b, _zigzag(400)], 2000, cache)
    assert calls == [], "unchanged trace must recompute nothing"

    calls.clear()
    _decimate_segments([closed_a, closed_b, _zigzag(460)], 2000, cache)
    assert len(calls) == 1, "only the grown segment should be recomputed"
    assert calls[0] == 460


def test_cached_output_still_tracks_a_shifting_budget() -> None:
    """`budget` is derived from the TOTAL point count, so a closed segment's
    budget shrinks as a later segment grows even though the segment itself is
    untouched. The memo holds only the budget-independent RDP result, so the
    cap has to keep being applied fresh on top of it — this pins that a cached
    run still equals a cold recompute after the budget has moved."""
    closed = _zigzag(600)
    cache: dict = {}
    _decimate_segments([closed, _zigzag(100)], 700, cache)
    # Grow the second segment a lot: `total` rises, so `closed`'s share falls.
    segments = [closed, _zigzag(5000)]
    assert _decimate_segments(segments, 700, cache) == _decimate_segments(segments, 700)


def test_saved_paths_are_simplified_but_endpoints_and_cursor_are_not() -> None:
    """`_paths_for_save` must shrink the on-disk copy without moving endpoints,
    and must leave `path_seen` (a RAW-point diff cursor) alone."""
    coord = object.__new__(coordinator_mod.AnyVacCoordinator)
    # A long straight run: nearly every interior point is redundant.
    straight = [{"x": float(i), "y": 0.0} for i in range(0, 3000, 3)]
    coord._dry_path = {"d1": [straight]}
    coord._wet_path = {"d1": []}
    coord._path_seen = {"d1": {"dry": len(straight), "wet": 0}}
    coord._dry_path_open = {"d1": True}
    coord._wet_path_open = {"d1": False}

    saved = coord._paths_for_save()
    seg = saved["d1"]["dry_path"][0]

    assert len(seg) < len(straight) / 10, "a straight run should collapse hard"
    assert seg[0] == straight[0] and seg[-1] == straight[-1]
    assert saved["d1"]["path_seen"] == {"dry": len(straight), "wet": 0}

    raw_bytes = len(json.dumps([straight]))
    saved_bytes = len(json.dumps(saved["d1"]["dry_path"]))
    assert saved_bytes < raw_bytes / 10


def test_saved_paths_keep_real_corners() -> None:
    """The flip side: simplification must not flatten a path that has genuine
    shape — a zigzag has no redundant points to drop."""
    coord = object.__new__(coordinator_mod.AnyVacCoordinator)
    zig = _zigzag(500)
    coord._dry_path = {"d1": [zig]}
    coord._wet_path = {"d1": []}
    coord._path_seen = {"d1": {"dry": len(zig), "wet": 0}}
    coord._dry_path_open = {"d1": False}
    coord._wet_path_open = {"d1": False}

    seg = coord._paths_for_save()["d1"]["dry_path"][0]
    assert seg == zig, "no point here is within epsilon of its neighbours' line"


def test_rdp_epsilon_is_below_positioning_precision() -> None:
    """Guards the premise the whole simplification rests on: a point is only
    dropped when it sits within `_RDP_EPSILON_MM` of the line through its
    surviving neighbours, which is well under the robot's own accuracy — so a
    restored trace can never be visibly different."""
    assert _RDP_EPSILON_MM <= 25.0
    # A deliberate 100 mm bulge must survive; a 5 mm one need not.
    big = [{"x": 0.0, "y": 0.0}, {"x": 500.0, "y": 100.0}, {"x": 1000.0, "y": 0.0}]
    small = [{"x": 0.0, "y": 0.0}, {"x": 500.0, "y": 5.0}, {"x": 1000.0, "y": 0.0}]
    assert len(_rdp_simplify(big, _RDP_EPSILON_MM)) == 3
    assert len(_rdp_simplify(small, _RDP_EPSILON_MM)) == 2
