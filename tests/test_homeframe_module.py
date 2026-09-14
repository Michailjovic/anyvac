"""Unit tests for `custom_components/anyvac/homeframe.py`'s persistence layer
(docs/40 §4.2, prompt §4.5) — pure functions, no `Store`/HA involved (that
wiring is `coordinator.py`'s job, tested separately in
`test_homeframe_persistence.py`).

Covers: `pack_mask`/`unpack_mask` round-trip, `frame_to_storage`/
`frame_from_storage` round-trip (masks, robot records, JSON-safe types),
`frames_to_storage`/`frames_from_storage` whole-store round-trip, malformed/
corrupt-data resilience (one bad frame or robot record never crashes the
load or takes healthy state down with it), and `grow_frame_canvas` (origin
never moves, old content preserved, new cells `False`, shrinking rejected).
"""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.anyvac import homeframe as hf


def _sample_frame(width: int = 40, height: int = 30, seed: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    return {
        "origin_mm": (-5000.0, -3000.0),
        "cell_mm": 50.0,
        "scale": 4.0,
        "width": width,
        "height": height,
        "epoch": 1,
        "floor_mask": rng.random((height, width)) > 0.5,
        "wall_mask": rng.random((height, width)) > 0.7,
        "robots": {
            "s7": {
                "rot_deg": 0.0, "tx_mm": 0.0, "ty_mm": 0.0, "score": 1.0, "iou": 1.0,
                "method": "reference", "map_index": 5, "map_sequence": 2,
                "grid_sha1": "abc123", "updated": "2026-09-13T12:00:00+00:00",
            },
            "s6": {
                "rot_deg": 179.0, "tx_mm": 46275.9, "ty_mm": 43826.1, "score": 0.925,
                "iou": 0.884, "method": "fine", "map_index": 3763, "map_sequence": 2,
                "grid_sha1": "def456", "updated": "2026-09-13T12:00:05+00:00",
            },
        },
    }


# ── pack_mask / unpack_mask ────────────────────────────────────────────────────


def test_pack_unpack_mask_round_trips() -> None:
    rng = np.random.default_rng(7)
    mask = rng.random((13, 27)) > 0.6
    assert np.array_equal(hf.unpack_mask(hf.pack_mask(mask), 13, 27), mask)


def test_pack_unpack_mask_all_false_and_all_true() -> None:
    for mask in (np.zeros((5, 9), dtype=bool), np.ones((5, 9), dtype=bool)):
        assert np.array_equal(hf.unpack_mask(hf.pack_mask(mask), 5, 9), mask)


# ── frame_to_storage / frame_from_storage ──────────────────────────────────────


def test_frame_to_storage_is_json_safe() -> None:
    stored = hf.frame_to_storage(_sample_frame())
    assert isinstance(stored["floor_mask"], str)
    assert isinstance(stored["wall_mask"], str)
    assert isinstance(stored["origin_mm"], list)
    assert isinstance(stored["width"], int) and isinstance(stored["height"], int)


def test_frame_round_trip_preserves_everything() -> None:
    frame = _sample_frame()
    back = hf.frame_from_storage(hf.frame_to_storage(frame))
    assert back["origin_mm"] == frame["origin_mm"]
    assert (back["width"], back["height"]) == (frame["width"], frame["height"])
    assert back["epoch"] == frame["epoch"]
    assert np.array_equal(back["floor_mask"], frame["floor_mask"])
    assert np.array_equal(back["wall_mask"], frame["wall_mask"])
    assert back["robots"] == frame["robots"]


def test_frame_from_storage_rejects_non_dict() -> None:
    assert hf.frame_from_storage("not a dict") is None
    assert hf.frame_from_storage(None) is None
    assert hf.frame_from_storage([1, 2, 3]) is None


def test_frame_from_storage_rejects_bad_dimensions() -> None:
    stored = hf.frame_to_storage(_sample_frame())
    stored["width"] = 0
    assert hf.frame_from_storage(stored) is None


def test_frame_from_storage_rejects_undecodable_mask() -> None:
    stored = hf.frame_to_storage(_sample_frame())
    stored["floor_mask"] = "not valid base64!!"
    assert hf.frame_from_storage(stored) is None


def test_frame_from_storage_drops_robot_record_missing_geometry() -> None:
    """A record without rot_deg/tx_mm/ty_mm is unusable — dropping it (never
    defaulting to 0,0,0) is the safe choice: a silent default would silently
    misplace that robot instead of just omitting it."""
    stored = hf.frame_to_storage(_sample_frame())
    stored["robots"]["s9"] = {"score": 0.9}  # no rot_deg/tx_mm/ty_mm
    back = hf.frame_from_storage(stored)
    assert "s9" not in back["robots"]
    assert set(back["robots"]) == {"s7", "s6"}


def test_frame_from_storage_ignores_non_dict_robot_records() -> None:
    stored = hf.frame_to_storage(_sample_frame())
    stored["robots"]["s9"] = "oops"
    back = hf.frame_from_storage(stored)
    assert "s9" not in back["robots"]


# ── frames_to_storage / frames_from_storage (whole store) ─────────────────────


def test_frames_round_trip_identical_state() -> None:
    frame = _sample_frame()
    frames = {"frame-a": frame}
    robot_frame = {"s7": "frame-a", "s6": "frame-a"}
    frames2, robot_frame2 = hf.frames_from_storage(hf.frames_to_storage(frames, robot_frame))
    assert set(frames2) == {"frame-a"}
    assert np.array_equal(frames2["frame-a"]["floor_mask"], frame["floor_mask"])
    assert robot_frame2 == robot_frame


def test_frames_from_storage_drops_one_corrupt_frame_keeps_the_rest() -> None:
    good = hf.frame_to_storage(_sample_frame())
    whole = {
        "frames": {
            "frame-a": good,
            "frame-corrupt": {"width": 5, "height": 5, "floor_mask": "!!not-b64", "wall_mask": "x"},
            "frame-not-a-dict": "oops",
        },
        "robot_frame": {"s7": "frame-a", "ghost": "frame-does-not-exist"},
    }
    frames, robot_frame = hf.frames_from_storage(whole)
    assert set(frames) == {"frame-a"}
    assert robot_frame == {"s7": "frame-a"}  # "ghost" -> a frame that doesn't exist is dropped


@pytest.mark.parametrize("bad_input", [None, {}, "oops", 42, []])
def test_frames_from_storage_never_raises_on_garbage(bad_input) -> None:
    frames, robot_frame = hf.frames_from_storage(bad_input)
    assert frames == {}
    assert robot_frame == {}


# ── grow_frame_canvas ──────────────────────────────────────────────────────────


def test_grow_frame_canvas_preserves_origin_and_content() -> None:
    frame = _sample_frame(width=40, height=30)
    grown = hf.grow_frame_canvas(frame, new_width=60, new_height=50)
    assert grown["origin_mm"] == frame["origin_mm"]
    assert (grown["width"], grown["height"]) == (60, 50)
    assert np.array_equal(grown["floor_mask"][0:30, 0:40], frame["floor_mask"])
    assert np.array_equal(grown["wall_mask"][0:30, 0:40], frame["wall_mask"])
    assert not grown["floor_mask"][0:30, 40:60].any()
    assert not grown["floor_mask"][30:50, :].any()
    assert grown["robots"] == frame["robots"]


def test_grow_frame_canvas_does_not_mutate_the_original() -> None:
    frame = _sample_frame(width=40, height=30)
    original_floor = frame["floor_mask"].copy()
    hf.grow_frame_canvas(frame, new_width=60, new_height=50)
    assert frame["width"] == 40 and frame["height"] == 30
    assert np.array_equal(frame["floor_mask"], original_floor)


def test_grow_frame_canvas_same_size_is_a_no_op_copy() -> None:
    frame = _sample_frame(width=40, height=30)
    grown = hf.grow_frame_canvas(frame, new_width=40, new_height=30)
    assert np.array_equal(grown["floor_mask"], frame["floor_mask"])


@pytest.mark.parametrize("new_width,new_height", [(10, 30), (40, 10), (5, 5)])
def test_grow_frame_canvas_rejects_shrinking(new_width, new_height) -> None:
    frame = _sample_frame(width=40, height=30)
    with pytest.raises(ValueError):
        hf.grow_frame_canvas(frame, new_width=new_width, new_height=new_height)
