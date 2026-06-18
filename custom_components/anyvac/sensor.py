"""AnyVac map sensor — one per discovered Roborock vacuum.

The sensor state is the number of (decimated) path points, which changes as the
robot cleans, so it is a cheap "is this map live" indicator. The real payload is
in the attributes: vacuum_position, charger, calibration_points, path, rooms and
image_dims. Because the ``path`` attribute can be large, exclude this entity from
the recorder (see README).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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


class AnyVacMapSensor(CoordinatorEntity[AnyVacCoordinator], SensorEntity):
    """Exposes parsed map data for one vacuum as state attributes."""

    _attr_has_entity_name = True
    _attr_name = "AnyVac Map"
    _attr_icon = "mdi:map-marker-path"

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
        return device.data
