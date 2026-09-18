"""Tests for docs/41 §4.6 — the Align mode manual floorplan seating override
layer: `AnyVacCoordinator.set_floorplan_seat`/`floorplan_seats`, the
`anyvac.set_floorplan_seat` service schema (validation), the `_async_setup`
load path (defensive against corrupted/malformed stored data, same posture
as the `room_pins` migration path in `test_room_pins.py`), and publication
on the sensor's `extra_state_attributes`.

Like `test_room_pins.py`, coordinators are built via `object.__new__` to
skip `__init__` — only the state each code path under test actually
touches is initialised.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol

from custom_components.anyvac.coordinator import AnyVacCoordinator
from custom_components.anyvac.sensor import AnyVacMapSensor
from custom_components.anyvac.services import SET_FLOORPLAN_SEAT_SCHEMA


class _FakeStore:
    """No-op stand-in for homeassistant.helpers.storage.Store, optionally
    returning a preset value from `async_load` for the reload tests."""

    def __init__(self, load_value: Any = None) -> None:
        self._load_value = load_value
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self._load_value

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        self.saved = get_data()


def _bare_coordinator() -> AnyVacCoordinator:
    """A coordinator with only `floorplan_seats`/`set_floorplan_seat`'s own
    state set up. `_listeners` is DataUpdateCoordinator's own state, needed
    because `set_floorplan_seat` calls `async_update_listeners()`."""
    coord = object.__new__(AnyVacCoordinator)
    coord._floorplan_seats = {}
    coord._seats_store = _FakeStore()
    coord._listeners = {}
    return coord


_SEAT_A = {
    "rotation": 91.5,
    "scale": 312.4,
    "offset_x": -3.25,
    "offset_y": 1.1,
}


# -- set_floorplan_seat / floorplan_seats (per-vacuum) ------------------------


def test_set_floorplan_seat_stores_one_vacuum() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    seats = coord.floorplan_seats
    assert seats["/local/anyvac/floor.png"]["vacuums"] == {"vacuum.s6": _SEAT_A}
    assert seats["/local/anyvac/floor.png"]["image_base"] is None
    assert seats["/local/anyvac/floor.png"]["updated"]


def test_set_floorplan_seat_rounds_to_0_01() -> None:
    """docs/41 §5 bod 4 — stored precision is 0.01, whatever precision the
    caller sent (the card's `seatToYaml` already rounds the same way, but
    the backend must not trust the caller)."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        map={
            "rotation": 91.5049,
            "scale": 312.396,
            "scale_y": 300.005,
            "offset_x": -3.254999,
            "offset_y": 1.105,
        },
    )
    m = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert m == {
        "rotation": 91.5,
        "scale": 312.4,
        "scale_y": 300.0,
        "offset_x": -3.25,
        "offset_y": 1.1,
    }


def test_set_floorplan_seat_omits_scale_y_when_not_given() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    m = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert "scale_y" not in m


def test_set_floorplan_seat_second_vacuum_does_not_touch_first() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s7",
        map={"rotation": 0, "scale": 100, "offset_x": 0, "offset_y": 0},
    )
    vacs = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]
    assert set(vacs) == {"vacuum.s6", "vacuum.s7"}
    assert vacs["vacuum.s6"] == _SEAT_A


def test_set_floorplan_seat_map_none_clears_just_that_vacuum() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s7",
        map={"rotation": 0, "scale": 100, "offset_x": 0, "offset_y": 0},
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=None)
    vacs = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]
    assert set(vacs) == {"vacuum.s7"}


def test_set_floorplan_seat_last_vacuum_gone_and_no_image_base_prunes_entry() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=None)
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_map_none_for_unknown_floorplan_is_a_harmless_noop() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/nope.png", vacuum="vacuum.s6", map=None)
    assert coord.floorplan_seats == {}


# -- set_floorplan_seat / floorplan_seats (card-level image_base) -------------


def test_set_floorplan_seat_vacuum_omitted_targets_image_base() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", image_base={"crop_box": {"x0": 1, "y0": 2, "x1": 3, "y1": 4}}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["image_base"] == {"crop_box": {"x0": 1, "y0": 2, "x1": 3, "y1": 4}}
    assert entry["vacuums"] == {}


def test_set_floorplan_seat_image_base_is_opaque_and_untouched() -> None:
    """image_base's internal shape (crop_box/home_anchors, phase G) is never
    interpreted at this layer — whatever dict comes in goes back out as-is."""
    coord = _bare_coordinator()
    payload = {"home_anchors": [1, 2, 3], "home_anchors_frame_id": "abc", "anything": True}
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base=payload)
    assert coord.floorplan_seats["/local/anyvac/floor.png"]["image_base"] == payload


def test_set_floorplan_seat_image_base_none_clears_it_but_keeps_vacuums() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base={"crop_box": {}})
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base=None)
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["image_base"] is None
    assert entry["vacuums"] == {"vacuum.s6": _SEAT_A}


def test_set_floorplan_seat_image_base_none_with_no_vacuums_prunes_entry() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base={"crop_box": {}})
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base=None)
    assert coord.floorplan_seats == {}


# -- floorplan_seats property returns a copy ----------------------------------


def test_floorplan_seats_property_returns_a_copy() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    snapshot = coord.floorplan_seats
    snapshot["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rotation"] = 999
    snapshot["/local/anyvac/floor.png"]["vacuums"]["vacuum.tampered"] = {}
    snapshot["/local/anyvac/other.png"] = {"vacuums": {}, "image_base": None}
    assert coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"] == {"vacuum.s6": _SEAT_A}
    assert "/local/anyvac/other.png" not in coord.floorplan_seats


# -- persisted store, independent per floorplan (two srcs don't collide) -----


def test_two_floorplans_are_independent() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor-a.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat(
        "/local/anyvac/floor-b.png",
        vacuum="vacuum.s6",
        map={"rotation": 0, "scale": 100, "offset_x": 0, "offset_y": 0},
    )
    assert set(coord.floorplan_seats) == {"/local/anyvac/floor-a.png", "/local/anyvac/floor-b.png"}
    coord.set_floorplan_seat("/local/anyvac/floor-a.png", vacuum="vacuum.s6", map=None)
    assert set(coord.floorplan_seats) == {"/local/anyvac/floor-b.png"}


# -- store reload (_async_setup) ----------------------------------------------


async def _load_seats(load_value: Any) -> dict[str, dict[str, Any]]:
    coord = object.__new__(AnyVacCoordinator)
    coord._store = _FakeStore()
    coord._sel_store = _FakeStore()
    coord._pins_store = _FakeStore()
    coord._seq_store = _FakeStore()
    coord._layers_store = _FakeStore()
    coord._seats_store = _FakeStore(load_value)
    coord._cov_store = _FakeStore()
    coord._cov_pct_store = _FakeStore()
    coord._est_store = _FakeStore()
    coord._paths_store = _FakeStore()
    coord._home_frame_store = _FakeStore()
    coord._floorplan_seats = {}  # __init__'s default, in case load_value isn't a dict
    await coord._async_setup()
    return coord._floorplan_seats


@pytest.mark.asyncio
async def test_reload_restores_a_well_formed_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": dict(_SEAT_A)},
            "image_base": {"crop_box": {"x0": 1}},
            "updated": "2026-09-17T12:00:00+00:00",
        }
    }
    seats = await _load_seats(stored)
    assert seats == stored


@pytest.mark.asyncio
async def test_reload_handles_no_stored_seats() -> None:
    assert await _load_seats(None) == {}


@pytest.mark.asyncio
async def test_reload_drops_entries_with_non_finite_or_non_positive_numbers() -> None:
    """Defensive load, same posture as `room_pins`'s migration path: a
    corrupted/hand-edited store must never crash `_async_setup` — bad
    per-vacuum entries are just dropped."""
    stored = {
        "/local/anyvac/bad-scale.png": {
            "vacuums": {"vacuum.s6": {"rotation": 0, "scale": 0, "offset_x": 0, "offset_y": 0}},
        },
        "/local/anyvac/bad-rotation.png": {
            "vacuums": {"vacuum.s6": {"rotation": float("nan"), "scale": 100, "offset_x": 0, "offset_y": 0}},
        },
        "/local/anyvac/bad-offset.png": {
            "vacuums": {"vacuum.s6": {"rotation": 0, "scale": 100, "offset_x": float("inf"), "offset_y": 0}},
        },
        "/local/anyvac/missing-field.png": {
            "vacuums": {"vacuum.s6": {"rotation": 0, "scale": 100, "offset_x": 0}},
        },
        "/local/anyvac/good.png": {
            "vacuums": {"vacuum.s6": dict(_SEAT_A)},
        },
    }
    seats = await _load_seats(stored)
    assert set(seats) == {"/local/anyvac/good.png"}


@pytest.mark.asyncio
async def test_reload_drops_non_finite_or_non_positive_scale_y_but_keeps_the_rest() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {**_SEAT_A, "scale_y": -5},
            },
        },
    }
    seats = await _load_seats(stored)
    m = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert "scale_y" not in m
    assert m["rotation"] == _SEAT_A["rotation"]


@pytest.mark.asyncio
async def test_reload_drops_malformed_shapes_without_crashing() -> None:
    stored = {
        "/local/anyvac/not-a-dict.png": "garbage",
        "/local/anyvac/vacuums-not-a-dict.png": {"vacuums": "garbage"},
        "/local/anyvac/vacuum-entry-not-a-dict.png": {"vacuums": {"vacuum.s6": "garbage"}},
        "/local/anyvac/image-base-not-a-dict.png": {"vacuums": {}, "image_base": "garbage"},
    }
    assert await _load_seats(stored) == {}


@pytest.mark.asyncio
async def test_reload_keeps_image_base_only_entry_with_no_vacuums() -> None:
    stored = {
        "/local/anyvac/floor.png": {"vacuums": {}, "image_base": {"crop_box": {"x0": 1}}},
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["image_base"] == {"crop_box": {"x0": 1}}


@pytest.mark.asyncio
async def test_reload_drops_entry_with_neither_vacuums_nor_image_base() -> None:
    stored = {"/local/anyvac/floor.png": {"vacuums": {}, "image_base": None}}
    assert await _load_seats(stored) == {}


# -- service schema validation -------------------------------------------------


def test_schema_accepts_a_well_formed_call() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "vacuum": "vacuum.s6", "map": _SEAT_A}
    )
    assert data["map"]["scale"] == pytest.approx(312.4)


def test_schema_requires_floorplan() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA({"vacuum": "vacuum.s6", "map": _SEAT_A})


def test_schema_allows_map_null_to_clear() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "vacuum": "vacuum.s6", "map": None}
    )
    assert data["map"] is None


def test_schema_allows_image_base_alone_without_vacuum() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "image_base": {"crop_box": {"x0": 1}}}
    )
    assert data["image_base"] == {"crop_box": {"x0": 1}}


def test_schema_rejects_non_positive_scale() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "map": {**_SEAT_A, "scale": 0},
            }
        )


def test_schema_rejects_nan_rotation() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "map": {**_SEAT_A, "rotation": float("nan")},
            }
        )


def test_schema_rejects_infinite_offset() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "map": {**_SEAT_A, "offset_x": float("inf")},
            }
        )


def test_schema_rejects_non_positive_scale_y() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "map": {**_SEAT_A, "scale_y": -1},
            }
        )


def test_schema_rejects_missing_required_map_field() -> None:
    incomplete = {k: v for k, v in _SEAT_A.items() if k != "offset_y"}
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {"floorplan": "/local/anyvac/floor.png", "vacuum": "vacuum.s6", "map": incomplete}
        )


def test_schema_rejects_missing_floorplan_entirely_even_with_other_fields() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA({"image_base": None})


# -- publication on the sensor's extra_state_attributes -----------------------


class _FakeDevice:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {"path_points": 1}


class _FakeCoordinatorForSensor:
    def __init__(self, floorplan_seats: dict[str, Any]) -> None:
        self.data = {"s6": _FakeDevice()}
        self.selected_rooms: list[str] = []
        self.view_layers = {"dry": True, "wet": False}
        self.room_pins: dict[str, Any] = {}
        self.room_sequence: dict[str, int] = {}
        self.floorplan_seats = floorplan_seats


def test_sensor_publishes_floorplan_seats() -> None:
    """`AnyVacMapSensor.extra_state_attributes` passes `floorplan_seats`
    through unchanged (docs/41 §4.6) — the card reads it straight off the
    sensor entity's attributes."""
    seats = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": dict(_SEAT_A)},
            "image_base": None,
            "updated": "2026-09-17T12:00:00+00:00",
        }
    }
    sensor = object.__new__(AnyVacMapSensor)
    sensor._duid = "s6"
    sensor.coordinator = _FakeCoordinatorForSensor(seats)
    attrs = sensor.extra_state_attributes
    assert attrs["floorplan_seats"] == seats


def test_floorplan_seats_is_in_unrecorded_attributes() -> None:
    """A missing recorder-exclusion for a large/fast-changing attribute means
    the user has to hand-write a `recorder: exclude` — the whole point of
    `_unrecorded_attributes` (see every other map-payload attribute here)."""
    assert "floorplan_seats" in AnyVacMapSensor._unrecorded_attributes
