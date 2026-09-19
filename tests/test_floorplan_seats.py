"""Tests for docs/41 §4.6 + docs/42 §9 (pre-H appearance extension) — the
Visual editor manual floorplan override layer:
`AnyVacCoordinator.set_floorplan_seat`/`floorplan_seats`, the
`anyvac.set_floorplan_seat` service schema (validation of `map` AND the new
`appearance` field), the `_async_setup` load path (defensive against
corrupted/malformed stored data — including the pre-1.12.0 flat per-vacuum
shape, which is discarded outright, no migration), and publication on the
sensor's `extra_state_attributes`.

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

_APPEARANCE_A = {
    "hide_map": True,
    "overlay_opacity": 55.0,
    "overlay_blend": "screen",
    "path_color": "#4fc3f7",
    "path_width": 140.0,
    "mop_path_color": None,
    "mop_band_opacity": 30.0,
    "mop_band_width": 220.0,
    "robot_image_on_map": True,
    "robot_size": 90.0,
    "robot_image_rotation": -15.0,
}


# -- set_floorplan_seat / floorplan_seats (per-vacuum map) --------------------


def test_set_floorplan_seat_stores_one_vacuum_map() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    seats = coord.floorplan_seats
    assert seats["/local/anyvac/floor.png"]["vacuums"] == {"vacuum.s6": {"map": _SEAT_A}}
    assert seats["/local/anyvac/floor.png"]["image_base"] is None
    assert seats["/local/anyvac/floor.png"]["updated"]


def test_set_floorplan_seat_rounds_map_to_0_01() -> None:
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
    m = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["map"]
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
    m = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["map"]
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
    assert vacs["vacuum.s6"]["map"] == _SEAT_A


def test_set_floorplan_seat_map_none_clears_just_the_map_of_that_vacuum() -> None:
    """No sentinel (docs/42 §8 bod 3): to clear ONLY `map` while keeping an
    existing `appearance`, a caller must resend `appearance` on the same
    call — omitting it is indistinguishable from `appearance=None` (see
    `test_set_floorplan_seat_appearance_omitted_also_clears_it` below). The
    card always sends both together on every Save, so this is exactly what
    it does in practice."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, appearance=_APPEARANCE_A
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=None, appearance=_APPEARANCE_A
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert "map" not in entry
    assert entry["appearance"] == _APPEARANCE_A


def test_set_floorplan_seat_map_none_for_unknown_floorplan_is_a_harmless_noop() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/nope.png", vacuum="vacuum.s6", map=None)
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_last_field_gone_and_no_image_base_prunes_entry() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=None)
    assert coord.floorplan_seats == {}


# -- set_floorplan_seat / floorplan_seats (per-vacuum appearance) -------------


def test_set_floorplan_seat_stores_one_vacuum_appearance() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", appearance=_APPEARANCE_A
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"appearance": _APPEARANCE_A}


def test_set_floorplan_seat_map_and_appearance_together_in_one_call() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        map=_SEAT_A,
        appearance=_APPEARANCE_A,
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A, "appearance": _APPEARANCE_A}


def test_set_floorplan_seat_appearance_none_clears_just_appearance_keeps_map() -> None:
    """Mirror of the map test above — resending `map` is what keeps it
    (no sentinel: an omitted field and an explicit `None` are the same
    thing, docs/42 §8 bod 3)."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, appearance=_APPEARANCE_A
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, appearance=None
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A}


def test_set_floorplan_seat_appearance_omitted_also_clears_it() -> None:
    """§4.6 "žádný sentinel" — an omitted `appearance` (the card always sends
    both together, but the low-level API doesn't require that) behaves
    exactly like an explicit `appearance=None`: clear."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, appearance=_APPEARANCE_A
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A}


def test_set_floorplan_seat_appearance_only_vacuum_prunes_when_both_cleared() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", appearance=_APPEARANCE_A
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", appearance=None)
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_appearance_stored_as_is_no_rounding() -> None:
    """Unlike `map`, appearance values are already validated/coerced by the
    service schema before reaching the coordinator — stored verbatim."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        appearance={"overlay_opacity": 55.4999, "path_width": 140.001},
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["appearance"]
    assert entry == {"overlay_opacity": 55.4999, "path_width": 140.001}


# -- set_floorplan_seat / floorplan_seats (per-vacuum rooms, docs/42 §4.4 fáze K) ---
#
# `rooms` deliberately does NOT follow the map/appearance "no sentinel, omit
# to clear" contract — it merges per room_key instead (see
# `AnyVacCoordinator.set_floorplan_seat`'s docstring).


_ROOM_KITCHEN = {"map_x": 12.5, "map_y": 30.0, "map_w": 40.0, "map_h": 25.0, "area_id": "kitchen"}
_ROOM_HALLWAY = {"map_x": 0.0, "map_y": 0.0, "map_w": 15.0, "map_h": 90.0}

_ROOM_STYLE_A = {"border_normal": 2.0, "border_selected": 4.0}


def test_set_floorplan_seat_stores_one_room_override() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": _ROOM_KITCHEN}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"rooms": {"kitchen": _ROOM_KITCHEN}}


def test_set_floorplan_seat_rooms_rounds_geometry_to_0_01() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        rooms={"kitchen": {"map_x": 12.5019999, "map_y": 30, "map_w": 40, "map_h": 25}},
    )
    room = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]["kitchen"]
    assert room["map_x"] == pytest.approx(12.5)


def test_set_floorplan_seat_second_room_key_does_not_touch_first() -> None:
    """Unlike map/appearance's whole-field clear-on-omit, `rooms` merges per
    key — adding "hallway" in a second call must leave "kitchen" untouched,
    even though "kitchen" isn't mentioned in that second call at all."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": _ROOM_KITCHEN}
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"hallway": _ROOM_HALLWAY}
    )
    rooms = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]
    assert rooms == {"kitchen": _ROOM_KITCHEN, "hallway": _ROOM_HALLWAY}


def test_set_floorplan_seat_room_key_null_clears_just_that_room() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        rooms={"kitchen": _ROOM_KITCHEN, "hallway": _ROOM_HALLWAY},
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": None}
    )
    rooms = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]
    assert rooms == {"hallway": _ROOM_HALLWAY}


def test_set_floorplan_seat_rooms_omitted_never_clears_anything() -> None:
    """The core asymmetry vs. map/appearance: a Save that doesn't mention
    `rooms` at all (e.g. a plain seat-geometry commit) must leave every
    existing room override completely untouched."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": _ROOM_KITCHEN}
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A)
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A, "rooms": {"kitchen": _ROOM_KITCHEN}}


def test_set_floorplan_seat_rooms_last_room_cleared_prunes_rooms_key_not_vacuum() -> None:
    """Clearing the only room override leaves an empty `rooms` dict, which is
    pruned away entirely — but a vacuum entry that still has `map` survives,
    as long as `map` is resent (it keeps its own OWN "no sentinel" contract —
    unrelated to `rooms`'s per-key merge — so it must still be resent on
    every call to survive, exactly like it always has)."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, rooms={"kitchen": _ROOM_KITCHEN}
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, rooms={"kitchen": None}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A}


def test_set_floorplan_seat_rooms_only_vacuum_prunes_entirely_when_last_room_cleared() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": _ROOM_KITCHEN}
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": None}
    )
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_rooms_area_id_none_clears_just_area_id() -> None:
    """A room dict's own `area_id: null` clears just that one field within
    the room override, distinct from `rooms: {key: null}` clearing the
    whole room — both null-shaped but at different levels."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": _ROOM_KITCHEN}
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        rooms={"kitchen": {**_ROOM_KITCHEN, "area_id": None}},
    )
    room = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]["kitchen"]
    assert room["area_id"] is None


def test_set_floorplan_seat_rooms_partial_room_dict_only_sets_given_fields() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"kitchen": {"area_id": "kitchen"}}
    )
    room = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]["kitchen"]
    assert room == {"area_id": "kitchen"}


def test_set_floorplan_seat_rooms_and_appearance_independent() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        vacuum="vacuum.s6",
        appearance=_APPEARANCE_A,
        rooms={"kitchen": _ROOM_KITCHEN},
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", vacuum="vacuum.s6", appearance=None)
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"rooms": {"kitchen": _ROOM_KITCHEN}}


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
    assert entry["vacuums"] == {"vacuum.s6": {"map": _SEAT_A}}


def test_set_floorplan_seat_image_base_none_with_no_vacuums_prunes_entry() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base={"crop_box": {}})
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base=None)
    assert coord.floorplan_seats == {}


# -- set_floorplan_seat / floorplan_seats (card-level rooms, docs/42 §4.4 fáze K) ---
#
# "vacuum given = per-vacuum rooms, omitted = card-level rooms (merged mode)"
# — the same split `image_base` already uses. Card-level `rooms` has the SAME
# per-room_key merge/omit-is-untouched semantics as the per-vacuum case.


def test_set_floorplan_seat_vacuum_omitted_with_rooms_targets_card_level_rooms() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}
    assert entry["vacuums"] == {}
    assert entry["image_base"] is None


def test_set_floorplan_seat_card_level_room_key_null_clears_just_that_room() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN, "hallway": _ROOM_HALLWAY}
    )
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": None})
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["rooms"] == {"hallway": _ROOM_HALLWAY}


def test_set_floorplan_seat_card_level_rooms_independent_of_image_base_when_both_resent() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        image_base={"crop_box": {"x0": 1}},
        rooms={"kitchen": _ROOM_KITCHEN},
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["image_base"] == {"crop_box": {"x0": 1}}
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}


def test_set_floorplan_seat_card_level_rooms_only_call_clears_image_base_if_not_resent() -> None:
    """Documents the one real footgun in this branch (see
    `AnyVacCoordinator.set_floorplan_seat`'s docstring): `image_base` keeps
    ITS OWN atomic "no sentinel" contract regardless of what else is in the
    same call — a `rooms`-only card-level Save still clears `image_base` if
    it isn't resent, exactly like it always has for any other card-level
    call. `rooms` not being cleared by an image_base-only call, in the next
    test, is the asymmetric part — `image_base` clearing on omission is not."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base={"crop_box": {"x0": 1}})
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN})
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["image_base"] is None
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}


def test_set_floorplan_seat_card_level_image_base_only_call_does_not_touch_rooms() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN})
    coord.set_floorplan_seat("/local/anyvac/floor.png", image_base={"crop_box": {"x0": 1}})
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}
    assert entry["image_base"] == {"crop_box": {"x0": 1}}


def test_set_floorplan_seat_card_level_rooms_and_per_vacuum_rooms_are_independent() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN})
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", rooms={"hallway": _ROOM_HALLWAY}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}
    assert entry["vacuums"]["vacuum.s6"]["rooms"] == {"hallway": _ROOM_HALLWAY}


def test_set_floorplan_seat_card_level_last_room_cleared_prunes_rooms_key() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN})
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": None})
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_card_level_last_room_cleared_keeps_image_base() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        image_base={"crop_box": {"x0": 1}},
        rooms={"kitchen": _ROOM_KITCHEN},
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", image_base={"crop_box": {"x0": 1}}, rooms={"kitchen": None}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry == {
        "vacuums": {},
        "image_base": {"crop_box": {"x0": 1}},
        "updated": entry["updated"],
    }


# -- set_floorplan_seat / floorplan_seats (card-level room_style, docs/42 §3.3
# / §9 fáze I addendum) --------------------------------------------------------
#
# Card-level only, whole-record "no sentinel" contract — same posture as
# `image_base`, NOT the per-room_key merge `rooms` uses (there's no room_key
# to key by: it's two global fields the card always edits/sends together).


def test_set_floorplan_seat_vacuum_omitted_with_room_style_targets_card_level() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style=_ROOM_STYLE_A)
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["room_style"] == _ROOM_STYLE_A
    assert entry["vacuums"] == {}
    assert entry["image_base"] is None


def test_set_floorplan_seat_room_style_ignored_when_vacuum_given() -> None:
    """`room_style` only ever has card-level meaning — a per-vacuum call
    that includes it must not create/touch the card-level entry's style."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, room_style=_ROOM_STYLE_A
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert "room_style" not in entry
    assert "room_style" not in entry["vacuums"]["vacuum.s6"]


def test_set_floorplan_seat_room_style_none_clears_it_but_keeps_image_base() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        image_base={"crop_box": {"x0": 1}},
        room_style=_ROOM_STYLE_A,
    )
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", image_base={"crop_box": {"x0": 1}}, room_style=None
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert "room_style" not in entry
    assert entry["image_base"] == {"crop_box": {"x0": 1}}


def test_set_floorplan_seat_room_style_omitted_also_clears_it() -> None:
    """Same atomic "no sentinel" rule `image_base` uses — a card-level call
    that only means to touch `rooms`/`image_base` must still resend
    `room_style` if it wants to keep an existing one."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style=_ROOM_STYLE_A)
    coord.set_floorplan_seat("/local/anyvac/floor.png", rooms={"kitchen": _ROOM_KITCHEN})
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert "room_style" not in entry
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}


def test_set_floorplan_seat_room_style_and_rooms_independent_when_both_resent() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png",
        rooms={"kitchen": _ROOM_KITCHEN},
        room_style=_ROOM_STYLE_A,
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["rooms"] == {"kitchen": _ROOM_KITCHEN}
    assert entry["room_style"] == _ROOM_STYLE_A


def test_set_floorplan_seat_room_style_only_prunes_entry_when_cleared() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style=_ROOM_STYLE_A)
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style=None)
    assert coord.floorplan_seats == {}


def test_set_floorplan_seat_room_style_only_entry_survives_pruning_check() -> None:
    """A `room_style`-only entry (no vacuums, no image_base, no rooms) must
    NOT be pruned — it holds something."""
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style=_ROOM_STYLE_A)
    assert "/local/anyvac/floor.png" in coord.floorplan_seats
    assert coord.floorplan_seats["/local/anyvac/floor.png"]["room_style"] == _ROOM_STYLE_A


def test_set_floorplan_seat_room_style_stored_as_is_no_rounding() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", room_style={"border_normal": 2.256, "border_selected": 4.789}
    )
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["room_style"] == {"border_normal": 2.256, "border_selected": 4.789}


def test_set_floorplan_seat_room_style_partial_dict_only_sets_given_fields() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat("/local/anyvac/floor.png", room_style={"border_normal": 3.0})
    entry = coord.floorplan_seats["/local/anyvac/floor.png"]
    assert entry["room_style"] == {"border_normal": 3.0}


# -- floorplan_seats property returns a copy ----------------------------------


def test_floorplan_seats_property_returns_a_copy() -> None:
    coord = _bare_coordinator()
    coord.set_floorplan_seat(
        "/local/anyvac/floor.png", vacuum="vacuum.s6", map=_SEAT_A, appearance=_APPEARANCE_A
    )
    snapshot = coord.floorplan_seats
    snapshot["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["map"]["rotation"] = 999
    snapshot["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["appearance"]["path_width"] = 1
    snapshot["/local/anyvac/floor.png"]["vacuums"]["vacuum.tampered"] = {}
    snapshot["/local/anyvac/other.png"] = {"vacuums": {}, "image_base": None}
    fresh = coord.floorplan_seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert fresh["map"] == _SEAT_A
    assert fresh["appearance"] == _APPEARANCE_A
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
async def test_reload_restores_a_well_formed_map_and_appearance_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": {"map": dict(_SEAT_A), "appearance": dict(_APPEARANCE_A)}},
            "image_base": {"crop_box": {"x0": 1}},
            "updated": "2026-09-17T12:00:00+00:00",
        }
    }
    seats = await _load_seats(stored)
    assert seats == stored


@pytest.mark.asyncio
async def test_reload_restores_appearance_only_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": {"appearance": dict(_APPEARANCE_A)}},
        }
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"] == {
        "appearance": _APPEARANCE_A
    }


@pytest.mark.asyncio
async def test_reload_handles_no_stored_seats() -> None:
    assert await _load_seats(None) == {}


@pytest.mark.asyncio
async def test_reload_discards_pre_1_12_0_flat_shape_no_migration() -> None:
    """BREAKING (integration 1.12.0, docs/42): a pre-1.12.0 stored entry —
    the flat `{rotation, scale, offset_x, offset_y}` per-vacuum dict, with
    no "map"/"appearance" wrapper keys at all — carries no salvageable
    information under the new shape and is discarded outright. No
    migration path is provided; this is a deliberate, documented reset."""
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": dict(_SEAT_A)},
        },
    }
    assert await _load_seats(stored) == {}


@pytest.mark.asyncio
async def test_reload_drops_entries_with_non_finite_or_non_positive_numbers() -> None:
    """Defensive load, same posture as `room_pins`'s migration path: a
    corrupted/hand-edited store must never crash `_async_setup` — bad
    per-vacuum entries are just dropped."""
    stored = {
        "/local/anyvac/bad-scale.png": {
            "vacuums": {"vacuum.s6": {"map": {"rotation": 0, "scale": 0, "offset_x": 0, "offset_y": 0}}},
        },
        "/local/anyvac/bad-rotation.png": {
            "vacuums": {
                "vacuum.s6": {
                    "map": {"rotation": float("nan"), "scale": 100, "offset_x": 0, "offset_y": 0}
                }
            },
        },
        "/local/anyvac/bad-offset.png": {
            "vacuums": {
                "vacuum.s6": {
                    "map": {"rotation": 0, "scale": 100, "offset_x": float("inf"), "offset_y": 0}
                }
            },
        },
        "/local/anyvac/missing-field.png": {
            "vacuums": {"vacuum.s6": {"map": {"rotation": 0, "scale": 100, "offset_x": 0}}},
        },
        "/local/anyvac/good.png": {
            "vacuums": {"vacuum.s6": {"map": dict(_SEAT_A)}},
        },
    }
    seats = await _load_seats(stored)
    assert set(seats) == {"/local/anyvac/good.png"}


@pytest.mark.asyncio
async def test_reload_drops_non_finite_or_non_positive_scale_y_but_keeps_the_rest() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {"map": {**_SEAT_A, "scale_y": -5}},
            },
        },
    }
    seats = await _load_seats(stored)
    m = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["map"]
    assert "scale_y" not in m
    assert m["rotation"] == _SEAT_A["rotation"]


@pytest.mark.asyncio
async def test_reload_drops_invalid_appearance_fields_but_keeps_valid_ones() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "appearance": {
                        "hide_map": True,
                        "overlay_opacity": 150,  # out of range 0-100
                        "overlay_blend": "not-a-real-blend-mode",
                        "path_color": "#4fc3f7",
                        "robot_size": float("nan"),
                    }
                }
            },
        },
    }
    seats = await _load_seats(stored)
    appearance = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["appearance"]
    assert appearance == {"hide_map": True, "path_color": "#4fc3f7"}


@pytest.mark.asyncio
async def test_reload_drops_appearance_entirely_invalid_leaves_map_only() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "map": dict(_SEAT_A),
                    "appearance": {"overlay_blend": "nonsense"},
                }
            },
        },
    }
    seats = await _load_seats(stored)
    entry = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A}


@pytest.mark.asyncio
async def test_reload_restores_a_well_formed_rooms_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "rooms": {"kitchen": dict(_ROOM_KITCHEN), "hallway": dict(_ROOM_HALLWAY)}
                }
            },
        }
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"] == {
        "kitchen": _ROOM_KITCHEN,
        "hallway": _ROOM_HALLWAY,
    }


@pytest.mark.asyncio
async def test_reload_drops_invalid_room_fields_but_keeps_valid_ones() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "rooms": {
                        "kitchen": {
                            "map_x": 12.5,
                            "map_w": -5,  # must be > 0
                            "map_h": float("nan"),
                            "area_id": "kitchen",
                        }
                    }
                }
            },
        },
    }
    seats = await _load_seats(stored)
    room = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"]["kitchen"]
    assert room == {"map_x": 12.5, "area_id": "kitchen"}


@pytest.mark.asyncio
async def test_reload_drops_one_bad_room_key_but_keeps_the_others() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "rooms": {
                        "kitchen": dict(_ROOM_KITCHEN),
                        "broken": "garbage",
                        "empty": {"map_w": -1},  # nothing survives re-validation
                    }
                }
            },
        },
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]["rooms"] == {
        "kitchen": _ROOM_KITCHEN
    }


@pytest.mark.asyncio
async def test_reload_drops_rooms_entirely_invalid_leaves_map_only() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {
                "vacuum.s6": {
                    "map": dict(_SEAT_A),
                    "rooms": {"kitchen": {"map_w": -1}},
                }
            },
        },
    }
    seats = await _load_seats(stored)
    entry = seats["/local/anyvac/floor.png"]["vacuums"]["vacuum.s6"]
    assert entry == {"map": _SEAT_A}


@pytest.mark.asyncio
async def test_reload_restores_a_well_formed_card_level_rooms_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {},
            "rooms": {"kitchen": dict(_ROOM_KITCHEN), "hallway": dict(_ROOM_HALLWAY)},
        }
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["rooms"] == {
        "kitchen": _ROOM_KITCHEN,
        "hallway": _ROOM_HALLWAY,
    }


@pytest.mark.asyncio
async def test_reload_drops_invalid_card_level_room_fields_but_keeps_valid_ones() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {},
            "rooms": {"kitchen": {"map_x": 12.5, "map_w": -5, "area_id": "kitchen"}},
        },
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["rooms"]["kitchen"] == {
        "map_x": 12.5,
        "area_id": "kitchen",
    }


@pytest.mark.asyncio
async def test_reload_keeps_card_level_rooms_only_entry_with_no_vacuums_or_image_base() -> None:
    stored = {"/local/anyvac/floor.png": {"vacuums": {}, "rooms": {"kitchen": dict(_ROOM_KITCHEN)}}}
    seats = await _load_seats(stored)
    assert set(seats) == {"/local/anyvac/floor.png"}
    assert seats["/local/anyvac/floor.png"]["rooms"] == {"kitchen": _ROOM_KITCHEN}


@pytest.mark.asyncio
async def test_reload_restores_a_well_formed_room_style_entry() -> None:
    stored = {
        "/local/anyvac/floor.png": {"vacuums": {}, "room_style": dict(_ROOM_STYLE_A)},
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["room_style"] == _ROOM_STYLE_A


@pytest.mark.asyncio
async def test_reload_drops_invalid_room_style_fields_but_keeps_valid_ones() -> None:
    stored = {
        "/local/anyvac/floor.png": {
            "vacuums": {},
            "room_style": {"border_normal": 3.0, "border_selected": -1},
        },
    }
    seats = await _load_seats(stored)
    assert seats["/local/anyvac/floor.png"]["room_style"] == {"border_normal": 3.0}


@pytest.mark.asyncio
async def test_reload_drops_room_style_out_of_range_leaves_entry_dropped_if_nothing_else() -> None:
    stored = {
        "/local/anyvac/floor.png": {"vacuums": {}, "room_style": {"border_normal": 999}},
    }
    seats = await _load_seats(stored)
    assert seats == {}


@pytest.mark.asyncio
async def test_reload_keeps_room_style_only_entry_with_no_vacuums_image_base_or_rooms() -> None:
    stored = {"/local/anyvac/floor.png": {"vacuums": {}, "room_style": dict(_ROOM_STYLE_A)}}
    seats = await _load_seats(stored)
    assert set(seats) == {"/local/anyvac/floor.png"}


@pytest.mark.asyncio
async def test_reload_drops_malformed_shapes_without_crashing() -> None:
    stored = {
        "/local/anyvac/not-a-dict.png": "garbage",
        "/local/anyvac/vacuums-not-a-dict.png": {"vacuums": "garbage"},
        "/local/anyvac/vacuum-entry-not-a-dict.png": {"vacuums": {"vacuum.s6": "garbage"}},
        "/local/anyvac/map-not-a-dict.png": {"vacuums": {"vacuum.s6": {"map": "garbage"}}},
        "/local/anyvac/appearance-not-a-dict.png": {
            "vacuums": {"vacuum.s6": {"appearance": "garbage"}}
        },
        "/local/anyvac/rooms-not-a-dict.png": {
            "vacuums": {"vacuum.s6": {"rooms": "garbage"}}
        },
        "/local/anyvac/room-entry-not-a-dict.png": {
            "vacuums": {"vacuum.s6": {"rooms": {"kitchen": "garbage"}}}
        },
        "/local/anyvac/image-base-not-a-dict.png": {"vacuums": {}, "image_base": "garbage"},
        "/local/anyvac/card-rooms-not-a-dict.png": {"vacuums": {}, "rooms": "garbage"},
        "/local/anyvac/card-room-entry-not-a-dict.png": {
            "vacuums": {},
            "rooms": {"kitchen": "garbage"},
        },
        "/local/anyvac/room-style-not-a-dict.png": {"vacuums": {}, "room_style": "garbage"},
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


# -- service schema validation: map ------------------------------------------


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


# -- service schema validation: appearance ------------------------------------


def test_schema_accepts_a_well_formed_appearance_call() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "appearance": _APPEARANCE_A,
        }
    )
    assert data["appearance"]["overlay_blend"] == "screen"
    assert data["appearance"]["mop_path_color"] is None


def test_schema_allows_appearance_null_to_clear() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "vacuum": "vacuum.s6", "appearance": None}
    )
    assert data["appearance"] is None


def test_schema_allows_map_and_appearance_together() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "map": _SEAT_A,
            "appearance": _APPEARANCE_A,
        }
    )
    assert data["map"] is not None
    assert data["appearance"] is not None


def test_schema_allows_partial_appearance_dict() -> None:
    """All appearance fields are individually optional — a caller (or the
    card) may send only the ones that changed... though in practice the
    card always sends the full draft (see services.yaml)."""
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "appearance": {"path_color": "#ffffff"},
        }
    )
    assert data["appearance"] == {"path_color": "#ffffff"}


def test_schema_rejects_invalid_overlay_blend() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"overlay_blend": "not-a-real-mode"},
            }
        )


def test_schema_rejects_overlay_opacity_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"overlay_opacity": 101},
            }
        )


def test_schema_rejects_path_width_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"path_width": 10},
            }
        )


def test_schema_rejects_mop_band_width_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"mop_band_width": 500},
            }
        )


def test_schema_rejects_robot_size_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"robot_size": 10},
            }
        )


def test_schema_rejects_robot_image_rotation_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"robot_image_rotation": 200},
            }
        )


def test_schema_rejects_non_bool_hide_map() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "appearance": {"hide_map": "yes"},
            }
        )


def test_schema_allows_null_path_color() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "appearance": {"path_color": None},
        }
    )
    assert data["appearance"]["path_color"] is None


# -- service schema validation: rooms (docs/42 §4.4 fáze K) -------------------


def test_schema_accepts_a_well_formed_rooms_call() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "rooms": {"kitchen": _ROOM_KITCHEN, "hallway": _ROOM_HALLWAY},
        }
    )
    assert data["rooms"]["kitchen"]["area_id"] == "kitchen"
    assert "area_id" not in data["rooms"]["hallway"]


def test_schema_allows_a_room_key_null_to_clear_just_that_room() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "rooms": {"kitchen": None},
        }
    )
    assert data["rooms"] == {"kitchen": None}


def test_schema_allows_map_appearance_and_rooms_together() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "map": _SEAT_A,
            "appearance": _APPEARANCE_A,
            "rooms": {"kitchen": _ROOM_KITCHEN},
        }
    )
    assert data["map"] is not None
    assert data["appearance"] is not None
    assert data["rooms"] is not None


def test_schema_allows_partial_room_dict() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "rooms": {"kitchen": {"area_id": "kitchen"}},
        }
    )
    assert data["rooms"] == {"kitchen": {"area_id": "kitchen"}}


def test_schema_allows_null_area_id_within_a_room() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "rooms": {"kitchen": {"area_id": None}},
        }
    )
    assert data["rooms"]["kitchen"]["area_id"] is None


def test_schema_rejects_non_positive_map_w() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": {**_ROOM_KITCHEN, "map_w": 0}},
            }
        )


def test_schema_rejects_non_positive_map_h() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": {**_ROOM_KITCHEN, "map_h": -5}},
            }
        )


def test_schema_rejects_nan_map_x() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": {**_ROOM_KITCHEN, "map_x": float("nan")}},
            }
        )


def test_schema_rejects_infinite_map_y() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": {**_ROOM_KITCHEN, "map_y": float("inf")}},
            }
        )


def test_schema_rejects_non_string_area_id() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": {"area_id": 42}},
            }
        )


def test_schema_rejects_non_dict_room_value_that_isnt_null() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {
                "floorplan": "/local/anyvac/floor.png",
                "vacuum": "vacuum.s6",
                "rooms": {"kitchen": "not-a-dict-or-null"},
            }
        )


def test_schema_allows_rooms_with_vacuum_omitted_card_level() -> None:
    """docs/42 §4.4 — `rooms` is valid with `vacuum` omitted (card-level,
    merged mode), unlike `map`/`appearance` which only ever have per-vacuum
    meaning."""
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "rooms": {"kitchen": _ROOM_KITCHEN}}
    )
    assert data["rooms"]["kitchen"]["area_id"] == "kitchen"
    assert "vacuum" not in data


def test_schema_allows_rooms_and_image_base_together_card_level() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "rooms": {"kitchen": _ROOM_KITCHEN},
            "image_base": {"crop_box": {"x0": 1}},
        }
    )
    assert data["rooms"] is not None
    assert data["image_base"] is not None


# -- service schema validation: room_style (docs/42 §3.3/§9 fáze I addendum) --


def test_schema_accepts_a_well_formed_room_style_call() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "room_style": _ROOM_STYLE_A}
    )
    assert data["room_style"] == _ROOM_STYLE_A
    assert "vacuum" not in data


def test_schema_allows_room_style_null_to_clear() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "room_style": None}
    )
    assert data["room_style"] is None


def test_schema_allows_partial_room_style_dict() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {"floorplan": "/local/anyvac/floor.png", "room_style": {"border_selected": 4}}
    )
    assert data["room_style"] == {"border_selected": 4.0}


def test_schema_allows_room_style_and_rooms_and_image_base_together() -> None:
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "rooms": {"kitchen": _ROOM_KITCHEN},
            "image_base": {"crop_box": {"x0": 1}},
            "room_style": _ROOM_STYLE_A,
        }
    )
    assert data["rooms"] is not None
    assert data["image_base"] is not None
    assert data["room_style"] is not None


def test_schema_rejects_negative_border_normal() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {"floorplan": "/local/anyvac/floor.png", "room_style": {"border_normal": -1}}
        )


def test_schema_rejects_border_selected_out_of_range() -> None:
    with pytest.raises(vol.Invalid):
        SET_FLOORPLAN_SEAT_SCHEMA(
            {"floorplan": "/local/anyvac/floor.png", "room_style": {"border_selected": 13}}
        )


def test_schema_allows_room_style_with_vacuum_given_but_coordinator_ignores_it() -> None:
    """Schema-level acceptance doesn't imply coordinator-level effect — see
    `test_set_floorplan_seat_room_style_ignored_when_vacuum_given` above for
    the actual ignore behaviour; the schema itself has no cross-field rule
    tying `room_style` to `vacuum`'s absence."""
    data = SET_FLOORPLAN_SEAT_SCHEMA(
        {
            "floorplan": "/local/anyvac/floor.png",
            "vacuum": "vacuum.s6",
            "room_style": _ROOM_STYLE_A,
        }
    )
    assert data["room_style"] == _ROOM_STYLE_A


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
    through unchanged (docs/41 §4.6 + docs/42) — the card reads it straight
    off the sensor entity's attributes."""
    seats = {
        "/local/anyvac/floor.png": {
            "vacuums": {"vacuum.s6": {"map": dict(_SEAT_A), "appearance": dict(_APPEARANCE_A)}},
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
