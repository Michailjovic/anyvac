"""Named service points per vacuum (docs/52 §5 item 7).

The coordinator keeps the points in robot mm and publishes them with the same
point in image px / home px and the room it lies in; the service handlers are
closures, so what is tested is the logic factored out of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.anyvac.const import SERVICE_POINTS_MAX
from custom_components.anyvac.coordinator import (
    AnyVacCoordinator,
    _parse_service_points,
    _room_at,
)
from custom_components.anyvac.services import (
    _current_position_mm,
    _job_rooms_for,
    _pick_service_point,
)


def _coord() -> AnyVacCoordinator:
    c = object.__new__(AnyVacCoordinator)
    c._service_points = {}
    c.saves = 0

    def _save() -> None:
        c.saves += 1

    c._save_service_points = _save  # type: ignore[method-assign]
    c._robot_frame = {}
    c._home_frames = {}
    return c


ROOMS = [
    {"name": "Kitchen", "x0": 0, "y0": 0, "x1": 4000, "y1": 3000},
    {"name": "Corridor", "x0": 3500, "y0": 0, "x1": 5000, "y1": 6000},
    {"name": "Pantry", "x0": 3600, "y0": 100, "x1": 3900, "y1": 900},
]


def test_set_adds_replaces_by_name_and_keeps_order() -> None:
    c = _coord()
    c.set_service_point("s6", "Trash bin", 1200.4, 800.6)
    c.set_service_point("s6", "Window", 100, 200)
    c.set_service_point("s6", " Trash bin ", 1500, 900)  # same name, trimmed
    assert c.service_points("s6") == [
        {"name": "Trash bin", "x": 1500, "y": 900},
        {"name": "Window", "x": 100, "y": 200},
    ]
    assert c.saves == 3
    assert c.service_points("s8") == []


def test_set_rejects_bad_name_and_too_many_points() -> None:
    c = _coord()
    with pytest.raises(ValueError):
        c.set_service_point("s6", "   ", 0, 0)
    with pytest.raises(ValueError):
        c.set_service_point("s6", "x" * 41, 0, 0)
    for i in range(SERVICE_POINTS_MAX):
        c.set_service_point("s6", f"p{i}", i, i)
    with pytest.raises(ValueError):
        c.set_service_point("s6", "one more", 0, 0)
    c.set_service_point("s6", "p0", 9, 9)  # replacing still works at the limit
    assert c.service_points("s6")[0] == {"name": "p0", "x": 9, "y": 9}


def test_remove_one_or_all() -> None:
    c = _coord()
    c.set_service_point("s6", "A", 1, 1)
    c.set_service_point("s6", "B", 2, 2)
    assert c.remove_service_point("s6", "A") == ["A"]
    assert [p["name"] for p in c.service_points("s6")] == ["B"]
    assert c.remove_service_point("s6", "missing") == []
    assert c.remove_service_point("s6") == ["B"]
    assert c.service_points("s6") == []


def test_parse_drops_malformed_entries_not_the_store() -> None:
    stored = {
        "s6": [
            {"name": "Bin", "x": 10, "y": 20},
            {"name": "Bin", "x": 30, "y": 40},  # duplicate name
            {"name": "", "x": 1, "y": 1},
            {"name": "NaN", "x": "nan", "y": 1},
            {"x": 1, "y": 1},
            "garbage",
        ],
        "s7": "garbage",
        "s8": [],
    }
    assert _parse_service_points(stored) == {"s6": [{"name": "Bin", "x": 10, "y": 20}]}
    assert _parse_service_points(None) == {}
    assert _parse_service_points(["list"]) == {}


def test_room_at_prefers_the_smallest_bbox() -> None:
    assert _room_at(ROOMS, 1000, 1000) == "Kitchen"
    assert _room_at(ROOMS, 3700, 500) == "Pantry"  # inside all three bboxes
    assert _room_at(ROOMS, 4500, 5000) == "Corridor"
    assert _room_at(ROOMS, 9000, 9000) is None
    assert _room_at([{"name": "Bad", "x0": None}], 0, 0) is None


def test_published_points_carry_px_home_px_and_room() -> None:
    c = _coord()
    c.set_service_point("s6", "Bin", 1000, 1000)
    c.mm_to_home_px = lambda duid, x, y: (x / 10 + 0.04, y / 10)  # type: ignore[method-assign]
    device = SimpleNamespace(duid="s6", data={"rooms": ROOMS})
    aff = (0.05, 0.0, 10.0, 0.0, -0.05, 500.0)  # px = 0.05*x + 10, py = -0.05*y + 500
    (p,) = c._published_service_points(device, aff)
    assert p["name"] == "Bin" and (p["x"], p["y"]) == (1000, 1000)
    assert p["px"]["x"] == pytest.approx(60.0) and p["px"]["y"] == pytest.approx(450.0)
    assert p["home_px"] == {"x": 100.0, "y": 100.0}
    assert p["room"] == "Kitchen"
    # No calibration and no home frame: the mm point is still published.
    c.mm_to_home_px = lambda duid, x, y: None  # type: ignore[method-assign]
    (p,) = c._published_service_points(device, None)
    assert p["px"] is None and p["home_px"] is None


def test_pick_point_by_name_or_the_only_one() -> None:
    one = [{"name": "Bin", "x": 1, "y": 2}]
    two = one + [{"name": "Window", "x": 3, "y": 4}]
    assert _pick_service_point(one, None, "s6")["name"] == "Bin"
    assert _pick_service_point(two, "Window", "s6")["x"] == 3
    with pytest.raises(HomeAssistantError, match="several"):
        _pick_service_point(two, None, "s6")
    with pytest.raises(HomeAssistantError, match="no service point 'Door'"):
        _pick_service_point(two, "Door", "s6")
    with pytest.raises(HomeAssistantError, match="has no service point"):
        _pick_service_point([], None, "s6")


class _Job:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def items(self) -> list[dict[str, Any]]:
        return self._items


def test_go_is_refused_only_for_rooms_still_to_do() -> None:
    job = _Job([
        {"duid": "s6", "state": "done", "rooms": ["Kitchen"]},
        {"duid": "s6", "state": "blocked", "rooms": ["Pantry"]},
        {"duid": "s6", "state": "removed", "rooms": ["Hall"]},
        {"duid": "s8", "state": "active", "rooms": ["Kitchen"]},
    ])
    assert _job_rooms_for([job], "s6") == []
    job._items.append({"duid": "s6", "state": "queued", "rooms": ["Corridor"]})
    job._items.append({"duid": "s6", "state": "sent", "rooms": ["Bathroom"]})
    assert _job_rooms_for([job], "s6") == ["Bathroom", "Corridor"]
    assert _job_rooms_for([], "s6") == []


def test_here_uses_the_last_known_position() -> None:
    coord = SimpleNamespace(data={"s6": SimpleNamespace(data={"vacuum_position": {"x": 1234, "y": -56, "a": 90}})})
    assert _current_position_mm(coord, "s6") == (1234, -56)
    coord.data["s6"].data["vacuum_position"] = None
    with pytest.raises(HomeAssistantError, match="not known"):
        _current_position_mm(coord, "s6")


def test_a_saved_point_is_published_without_waiting_for_a_poll() -> None:
    c = object.__new__(AnyVacCoordinator)
    c._service_points = {}
    c._robot_frame = {}
    c._home_frames = {}
    c._pending_saves = {}
    class _Store:
        def async_delay_save(self, func, delay) -> None:
            pass

    c._sp_store = _Store()
    device = SimpleNamespace(duid="s6", data={"rooms": ROOMS, "calibration_points": None})
    c.data = {"s6": device}
    c.async_update_listeners = lambda: None  # type: ignore[method-assign]
    c.set_service_point("s6", "Bin", 1000, 1000)
    assert device.data["service_points"][0]["room"] == "Kitchen"
    c.remove_service_point("s6")
    assert device.data["service_points"] == []
