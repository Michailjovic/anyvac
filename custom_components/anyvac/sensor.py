"""AnyVac map sensor — one per discovered Roborock vacuum.

The sensor state is the number of (decimated) path points, which changes as the
robot cleans, so it is a cheap "is this map live" indicator. The real payload is
in the attributes: vacuum_position, charger, calibration_points, path, rooms and
image_dims. Because the ``path`` attribute can be large, exclude this entity from
the recorder (see README).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, ROBOROCK_DOMAIN
from .coordinator import AnyVacCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AnyVac sensors, adding new vacuums as they are discovered."""
    coordinator: AnyVacCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new_entities = []
        for duid in coordinator.data or {}:
            if duid not in known:
                known.add(duid)
                new_entities.append(AnyVacMapSensor(coordinator, duid))
        if new_entities:
            async_add_entities(new_entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))

    known_rooms: set[str] = set()

    @callback
    def _add_room_sensors() -> None:
        new = []
        for room in coordinator.all_room_names():
            for kind in ("dry", "wet"):
                key = f"{room}|{kind}"
                if key not in known_rooms:
                    known_rooms.add(key)
                    new.append(AnyVacRoomTimeSensor(coordinator, room, kind))
        if new:
            async_add_entities(new)

    _add_room_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_room_sensors))


class AnyVacMapSensor(CoordinatorEntity[AnyVacCoordinator], SensorEntity):
    """Exposes parsed map data for one vacuum as state attributes."""

    _attr_has_entity_name = True
    _attr_name = "AnyVac Map"
    _attr_icon = "mdi:map-marker-path"
    # Keep the large / fast-changing map payload out of the recorder automatically,
    # so the user never has to add a `recorder: exclude` to configuration.yaml.
    _unrecorded_attributes = frozenset(
        {
            "vacuum_position", "charger", "calibration_points", "path", "mop_path", "rooms",
            "image_dims", "cleaned_rooms", "rooms_last_cleaned", "rooms_estimate",
            "vacuum_room", "vacuum_room_name", "in_cleaning", "clean_type", "mop_signal", "duid",
            "path_points", "mop_path_points", "calib_debug", "selected_rooms",
            "rooms_progress", "path_dry", "path_dry_points", "path_wet",
            "status_state", "transit",
        }
    )

    def __init__(self, coordinator: AnyVacCoordinator, duid: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._duid = duid
        device = coordinator.data[duid]
        self._attr_unique_id = f"{duid}_anyvac_map"
        # Attach to the existing Roborock device so the entity shows up there.
        self._attr_device_info = {
            "identifiers": {(ROBOROCK_DOMAIN, duid)},
        }

    @property
    def _device(self) -> Any:
        return (self.coordinator.data or {}).get(self._duid)

    @property
    def available(self) -> bool:
        """Available only while the vacuum is present in coordinator data."""
        return super().available and self._device is not None

    @property
    def native_value(self) -> int | None:
        """Number of path points currently known (a 'map is live' proxy)."""
        device = self._device
        if device is None:
            return None
        return len(device.data.get("path") or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The actual map payload consumed by the AnyVac card."""
        device = self._device
        if device is None:
            return None
        return {**device.data, "selected_rooms": self.coordinator.selected_rooms}


class AnyVacRoomTimeSensor(CoordinatorEntity[AnyVacCoordinator], SensorEntity):
    """Per-room 'last cleaned' timestamp (dry or wet) — for overdue notifications/automations."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AnyVacCoordinator, room: str, kind: str) -> None:
        """Initialize the room timestamp sensor."""
        super().__init__(coordinator)
        self._room = room
        self._kind = kind
        self._attr_name = f"{room} last {kind}"
        self._attr_unique_id = f"anyvac_room_{slugify(room)}_{kind}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "rooms")},
            "name": "AnyVac Rooms",
            "manufacturer": "AnyVac",
        }

    @property
    def native_value(self) -> datetime | None:
        """Timestamp of the last clean of this room for this clean type."""
        iso = (self.coordinator.rooms_history.get(self._room) or {}).get(self._kind)
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except ValueError:
            return None
