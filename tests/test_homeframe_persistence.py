"""Tests for docs/40 Fáze 1 home-frame persistence (`.storage/anyvac.home_frame`,
`homeassistant.helpers.storage.Store` version 1 — docs/40 §4.2, prompt §4.5
test category 4: "save → load → identický stav; růst frame v +x/+y bez
změny počátku").

Mirrors `test_path_persistence.py`'s own pattern exactly (`object.__new__` to
skip `__init__`, a `_FakeStore` that actually round-trips `async_delay_save`
-> `async_load` synchronously, so a second coordinator instance can be seeded
with the first's saved snapshot to simulate a restart) — this file only adds
the `_home_frame_store` piece, it does not re-test anything `_paths_store`'s
suite already covers.

The (de)serialisation itself (`pack_mask`/`unpack_mask`, `frame_to_storage`/
`frame_from_storage`, malformed-data resilience) is unit-tested directly
against `homeframe.py` in `test_homeframe_module.py` — this file only checks
that `coordinator.py`'s `Store` wiring (`_async_setup` load,
`_home_frame_for_save`) actually uses those functions correctly end to end.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from custom_components.anyvac import homeframe
from custom_components.anyvac.coordinator import AnyVacCoordinator


class _FakeBus:
    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        pass


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()


class _FakeStore:
    """Same contract as `test_path_persistence.py`'s own — `async_load`
    returns whatever was last `async_delay_save`d (or a preset seed value),
    synchronously, no real delay."""

    def __init__(self, load_value: Any = None) -> None:
        self._load_value = load_value
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self.saved if self.saved is not None else self._load_value

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        self.saved = get_data()


def _bare_coordinator(home_frame_store: _FakeStore | None = None) -> AnyVacCoordinator:
    """Just enough state for `_async_setup` to run its home-frame branch —
    every OTHER store it touches gets a plain no-op-ish `_FakeStore()` since
    this file only exercises the home-frame piece."""
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeHass()
    for attr in (
        "_store", "_est_store", "_cov_store", "_cov_pct_store", "_sel_store",
        "_pins_store", "_seq_store", "_layers_store", "_seats_store", "_paths_store",
    ):
        setattr(coord, attr, _FakeStore())
    coord._home_frame_store = home_frame_store or _FakeStore()
    # __init__'s defaults, in case the store is empty/missing (first run).
    coord._home_frames = {}
    coord._robot_frame = {}
    return coord


def _sample_frame(width: int = 40, height: int = 30) -> dict[str, Any]:
    rng = np.random.default_rng(3)
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


@pytest.mark.asyncio
async def test_home_frame_survives_a_simulated_restart() -> None:
    frame = _sample_frame()
    before = _bare_coordinator()
    before._home_frames = {"frame-a": frame}
    before._robot_frame = {"s7": "frame-a", "s6": "frame-a"}

    # What `_home_frame_for_save` would hand `Store.async_delay_save`.
    before._home_frame_store.async_delay_save(before._home_frame_for_save, 5)

    # Simulate a restart: a fresh coordinator, seeded with the FIRST one's
    # saved snapshot — exactly `_async_setup`'s real contract.
    after = _bare_coordinator(home_frame_store=before._home_frame_store)
    await after._async_setup()

    assert set(after._home_frames) == {"frame-a"}
    assert after._robot_frame == {"s7": "frame-a", "s6": "frame-a"}
    loaded = after._home_frames["frame-a"]
    assert loaded["origin_mm"] == frame["origin_mm"]
    assert (loaded["width"], loaded["height"]) == (40, 30)
    assert loaded["epoch"] == 1
    assert np.array_equal(loaded["floor_mask"], frame["floor_mask"])
    assert np.array_equal(loaded["wall_mask"], frame["wall_mask"])
    assert loaded["robots"] == frame["robots"]


@pytest.mark.asyncio
async def test_home_frame_grows_in_positive_axes_without_moving_origin() -> None:
    frame = _sample_frame(width=40, height=30)
    grown = homeframe.grow_frame_canvas(frame, new_width=60, new_height=50)

    before = _bare_coordinator()
    before._home_frames = {"frame-a": grown}
    before._robot_frame = {"s7": "frame-a"}
    before._home_frame_store.async_delay_save(before._home_frame_for_save, 5)

    after = _bare_coordinator(home_frame_store=before._home_frame_store)
    await after._async_setup()

    loaded = after._home_frames["frame-a"]
    # The whole point of docs/40 §4.1's "frame se nezmenšuje... origin je
    # pevný" — growth must never move existing content or the origin.
    assert loaded["origin_mm"] == frame["origin_mm"]
    assert (loaded["width"], loaded["height"]) == (60, 50)
    assert np.array_equal(loaded["floor_mask"][0:30, 0:40], frame["floor_mask"])
    assert not loaded["floor_mask"][0:30, 40:60].any()
    assert not loaded["floor_mask"][30:50, :].any()


@pytest.mark.asyncio
async def test_home_frame_first_run_has_no_stored_state() -> None:
    """No `.storage/anyvac.home_frame` file yet (fresh install) — `_async_setup`
    must leave the `__init__` defaults (empty dicts) alone, not crash."""
    coord = _bare_coordinator(home_frame_store=_FakeStore(load_value=None))
    await coord._async_setup()
    assert coord._home_frames == {}
    assert coord._robot_frame == {}


@pytest.mark.asyncio
async def test_home_frame_corrupt_store_data_does_not_crash_setup() -> None:
    """A hand-edited or half-written store file must not take down integration
    startup — `homeframe.frames_from_storage` already drops unusable frames
    (unit-tested directly); this checks the coordinator wiring doesn't
    reintroduce a crash on top of that (e.g. by not going through it)."""
    bad_store = _FakeStore(load_value={"frames": {"broken": {"width": "not-a-number"}}, "robot_frame": {}})
    coord = _bare_coordinator(home_frame_store=bad_store)
    await coord._async_setup()  # must not raise
    assert coord._home_frames == {}
    assert coord._robot_frame == {}


@pytest.mark.asyncio
async def test_home_frame_for_save_matches_homeframe_frames_to_storage() -> None:
    """`_home_frame_for_save` must be a thin, faithful wrapper — not a second,
    drifting implementation of the same serialisation (docs/14 rule 1)."""
    frame = _sample_frame()
    coord = _bare_coordinator()
    coord._home_frames = {"frame-a": frame}
    coord._robot_frame = {"s7": "frame-a"}
    assert coord._home_frame_for_save() == homeframe.frames_to_storage(
        coord._home_frames, coord._robot_frame
    )
