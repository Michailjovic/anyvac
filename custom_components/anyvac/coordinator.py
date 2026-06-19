"""Coordinator that reads parsed Roborock map data and normalises it.

Design note (piggyback): we deliberately do NOT open our own Roborock
connection. Instead we read the parsed ``MapData`` out of the official Roborock
integration's runtime coordinators:

    roborock config entry
      -> entry.runtime_data.v1            (list of v1 coordinators)
      -> coord.properties_api.home        (HomeTrait)
      -> home.home_map_content[flag]      (MapContent)
      -> map_content.map_data             (vacuum_map_parser MapData)

This couples us to the Roborock integration's internal structure, so every
access is defensive (getattr / try-except) and a missing/changed attribute just
yields no data for that vacuum rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PATH_MAX_POINTS, ROBOROCK_DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


@dataclass
class AnyVacDevice:
    """Normalised, card-ready map data for a single vacuum."""

    duid: str
    slug: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)


def _point(p: Any) -> dict[str, float] | None:
    """Convert a parser Point (x, y, optional angle a) to a plain dict."""
    if p is None:
        return None
    x = getattr(p, "x", None)
    y = getattr(p, "y", None)
    if x is None or y is None:
        return None
    out: dict[str, float] = {"x": x, "y": y}
    a = getattr(p, "a", None)
    if a is not None:
        out["a"] = a
    return out


def _decimate(points: list[Any], max_points: int) -> list[Any]:
    """Down-sample a list to at most ``max_points`` entries, keeping shape."""
    n = len(points)
    if n <= max_points or max_points <= 0:
        return points
    step = (n + max_points - 1) // max_points
    return points[::step]


def _extract_map(map_data: Any) -> dict[str, Any]:
    """Pull the fields the card needs out of a parser MapData object."""
    out: dict[str, Any] = {}

    out["vacuum_position"] = _point(getattr(map_data, "vacuum_position", None))
    out["charger"] = _point(getattr(map_data, "charger", None))

    # Calibration points (vacuum mm <-> map px), computed by the parser itself.
    try:
        out["calibration_points"] = map_data.calibration()
    except Exception as err:  # noqa: BLE001 - never let calibration break the update
        _LOGGER.debug("AnyVac: calibration() failed: %s", err)
        out["calibration_points"] = None

    # Cleaning path: list[list[Point]] -> flat decimated list of {x, y}.
    flat: list[dict[str, float]] = []
    path_obj = getattr(map_data, "path", None)
    subpaths = getattr(path_obj, "path", None) if path_obj is not None else None
    if subpaths:
        for sub in subpaths:
            for pt in sub:
                px = getattr(pt, "x", None)
                py = getattr(pt, "y", None)
                if px is not None and py is not None:
                    flat.append({"x": px, "y": py})
    out["path"] = _decimate(flat, PATH_MAX_POINTS)

    # Rooms: {segment_number: Room} -> list of plain dicts.
    rooms: list[dict[str, Any]] = []
    room_dict = getattr(map_data, "rooms", None) or {}
    for num, room in room_dict.items():
        rooms.append(
            {
                "segment_id": getattr(room, "number", num),
                "name": getattr(room, "name", None),
                "x0": getattr(room, "x0", None),
                "y0": getattr(room, "y0", None),
                "x1": getattr(room, "x1", None),
                "y1": getattr(room, "y1", None),
                "pos_x": getattr(room, "pos_x", None),
                "pos_y": getattr(room, "pos_y", None),
            }
        )
    out["rooms"] = rooms

    # Which segments the robot has cleaned (this session) + where it currently is.
    out["cleaned_rooms"] = sorted(getattr(map_data, "cleaned_rooms", None) or [])
    out["vacuum_room"] = getattr(map_data, "vacuum_room", None)
    out["vacuum_room_name"] = getattr(map_data, "vacuum_room_name", None)

    # Image dimensions (for mm <-> map-pixel conversions on the card side).
    img = getattr(map_data, "image", None)
    dims = getattr(img, "dimensions", None) if img is not None else None
    if dims is not None:
        out["image_dims"] = {
            "top": getattr(dims, "top", None),
            "left": getattr(dims, "left", None),
            "width": getattr(dims, "width", None),
            "height": getattr(dims, "height", None),
            "scale": getattr(dims, "scale", None),
            "rotation": getattr(dims, "rotation", None),
        }

    return out


class AnyVacCoordinator(DataUpdateCoordinator[dict[str, AnyVacDevice]]):
    """Periodically read parsed map data from the Roborock integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._store: Store = Store(hass, 1, f"{DOMAIN}_room_history")
        # Cross-vacuum room history keyed by room NAME: {name: {dry, wet, any: iso}}
        self._history: dict[str, dict[str, str]] = {}

    async def _async_setup(self) -> None:
        """Load persisted per-room clean history before the first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._history = {k: dict(v) for k, v in stored.items() if isinstance(v, dict)}

    def _history_for_save(self) -> dict[str, dict[str, str]]:
        return self._history

    def _update_history(self, device: AnyVacDevice) -> None:
        """While a vacuum is cleaning, stamp the room it is currently in (and any
        firmware-reported cleaned segments) with the current clean type, keyed by
        room NAME so history aggregates across all vacuums. Persisted across restarts.

        We use vacuum_room (presence) as the primary signal because cleaned_rooms is
        not reliably populated; cleaned_rooms is unioned in when present.
        """
        if not device.data.get("in_cleaning"):
            return
        names: set[str] = set()
        current = device.data.get("vacuum_room_name")
        if current:
            names.add(current)
        seg_to_name = {
            str(r.get("segment_id")): r.get("name") for r in device.data.get("rooms", [])
        }
        for seg in device.data.get("cleaned_rooms") or []:
            nm = seg_to_name.get(str(seg))
            if nm:
                names.add(nm)
        if not names:
            return
        ctype = device.data.get("clean_type")
        now = dt_util.utcnow().isoformat()
        for nm in names:
            rec = self._history.setdefault(nm, {})
            rec["any"] = now
            if ctype in ("dry", "wet"):
                rec[ctype] = now
        self._store.async_delay_save(self._history_for_save, 5)

    async def _async_update_data(self) -> dict[str, AnyVacDevice]:
        """Read every Roborock v1 coordinator and normalise its map data."""
        result: dict[str, AnyVacDevice] = {}
        for rb_entry in self.hass.config_entries.async_entries(ROBOROCK_DOMAIN):
            runtime = getattr(rb_entry, "runtime_data", None)
            coords = getattr(runtime, "v1", None) or []
            for coord in coords:
                try:
                    device = self._extract_device(coord)
                except Exception as err:  # noqa: BLE001 - one bad device must not fail the rest
                    _LOGGER.debug("AnyVac: failed reading a Roborock coordinator: %s", err)
                    continue
                if device is not None:
                    self._update_history(device)
                    for room in device.data.get("rooms", []):
                        rec = self._history.get(room.get("name")) or {}
                        room["last_cleaned"] = rec.get("any")
                        room["last_dry"] = rec.get("dry")
                        room["last_wet"] = rec.get("wet")
                    device.data["rooms_last_cleaned"] = {k: dict(v) for k, v in self._history.items()}
                    result[device.duid] = device
        return result

    def _extract_device(self, coord: Any) -> AnyVacDevice | None:
        """Extract normalised map data for one Roborock v1 coordinator."""
        home = getattr(getattr(coord, "properties_api", None), "home", None)
        if home is None:
            return None

        contents = getattr(home, "home_map_content", None) or {}
        # Prefer the current map; fall back to any cached map content.
        current = getattr(home, "current_map_data", None)
        flag = getattr(current, "map_flag", None)
        map_content = contents.get(flag) if flag is not None else None
        if map_content is None:
            map_content = next(iter(contents.values()), None)

        map_data = getattr(map_content, "map_data", None) if map_content else None
        if map_data is None:
            return None

        data = _extract_map(map_data)

        # MapData.rooms carry no names; merge them from the home trait's room mapping.
        names: dict[int, str] = {}
        try:
            for room_map in getattr(home, "current_rooms", None) or []:
                seg = getattr(room_map, "segment_id", None)
                nm = getattr(room_map, "name", None)
                if seg is not None and nm:
                    names[seg] = nm
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("AnyVac: failed reading room names: %s", err)
        for room in data.get("rooms", []):
            if room.get("name") is None and room.get("segment_id") in names:
                room["name"] = names[room["segment_id"]]

        status = getattr(getattr(coord, "properties_api", None), "status", None)

        def _s(attr: str) -> Any:
            return getattr(status, attr, None) if status is not None else None

        data["in_cleaning"] = bool(_s("in_cleaning"))
        water_name = _s("water_mode_name")
        data["mop_signal"] = {
            "fan_power": _s("fan_power"),
            "fan_speed_name": _s("fan_speed_name"),
            "water_box_mode": _s("water_box_mode"),
            "water_mode_name": water_name,
            "mop_mode": _s("mop_mode"),
            "mop_route_name": _s("mop_route_name"),
            "water_box_status": _s("water_box_status"),
            "water_box_carriage_status": _s("water_box_carriage_status"),
            "is_water_box_carriage_attached": _s("is_water_box_carriage_attached"),
        }
        # Best-effort dry/wet: "wet" when a water level is active. Verify via mop_signal
        # and we will refine the exact field once confirmed on hardware.
        data["clean_type"] = (
            "wet" if water_name and str(water_name).lower() not in ("off", "none", "closed") else "dry"
        )
        vr = data.get("vacuum_room")
        if vr is not None and not data.get("vacuum_room_name"):
            data["vacuum_room_name"] = names.get(vr)

        duid = getattr(coord, "duid", None) or "unknown"
        slug = getattr(coord, "duid_slug", None) or duid
        name = getattr(getattr(coord, "device", None), "name", None) or slug
        return AnyVacDevice(duid=duid, slug=slug, name=name, data=data)
