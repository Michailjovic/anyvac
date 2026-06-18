"""The AnyVac companion integration.

AnyVac sits on top of the official Roborock integration. The Roborock integration
already parses the vacuum map (via python-roborock / vacuum-map-parser) into a
structured ``MapData`` object containing the robot position, cleaning path, room
geometry and calibration points — but it never exposes that data as entities.

AnyVac reads that already-parsed data out of the Roborock integration's runtime
coordinators and re-publishes it as a sensor per vacuum, so the AnyVac card can
draw the robot and its path on a custom floorplan and run zone / pin-and-go with
no manual calibration.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import AnyVacCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AnyVac from a config entry."""
    coordinator = AnyVacCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AnyVac config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
