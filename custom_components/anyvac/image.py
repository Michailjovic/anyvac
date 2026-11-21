"""AnyVac reach mask image — one per discovered vacuum (docs/51 §2.5).

Where the robot's body has actually been: the swept footprint of its last
complete clean of every room, drawn on the robot's OWN map canvas (same size
and pixel space as its Roborock map image), so the card can lay it over that
map with exactly the same seat. Served through Home Assistant's image entity
(access-token protected), deliberately not as a `/local/` file — it is the
floor plan of the user's home.

Not attached to the Roborock device: the card auto-resolves a vacuum's map as
the only/live `image.*` on that device, and a second image there would make
that choice ambiguous for every card older than this one. The map sensor's
`reach.entity_id` attribute points at this entity instead.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import AnyVacCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one reach image per vacuum, also for vacuums discovered later."""
    coordinator: AnyVacCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for duid, device in (coordinator.data or {}).items():
            if duid not in known:
                known.add(duid)
                new.append(AnyVacReachImage(coordinator, duid, getattr(device, "name", None) or duid))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AnyVacReachImage(CoordinatorEntity[AnyVacCoordinator], ImageEntity):
    """The robot's reach mask as a transparent PNG (empty until the first
    complete clean of a room has been recorded)."""

    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:texture-box"

    def __init__(self, coordinator: AnyVacCoordinator, duid: str, name: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._duid = duid
        self._attr_unique_id = f"{duid}_anyvac_reach"
        self._attr_name = f"{name} reach mask"
        self._attr_image_last_updated = coordinator.reach_updated(duid)

    @property
    def available(self) -> bool:
        return super().available and (self.coordinator.data or {}).get(self._duid) is not None

    async def async_image(self) -> bytes | None:
        return self.coordinator.reach_png(self._duid)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_image_last_updated = self.coordinator.reach_updated(self._duid)
        super()._handle_coordinator_update()
