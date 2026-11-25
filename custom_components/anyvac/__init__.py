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

import hashlib
import logging
import os
import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_KEYS
from .coordinator import AnyVacCoordinator
from .services import ALL_SERVICES, _cancel_jobs, async_register_services, async_restore_job

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.IMAGE, Platform.SENSOR]


# Earlier bundled blueprint versions (sha256 of the file as shipped). An
# installed copy that still matches one was never edited and is replaced by the
# current version. 1.58.0: the clean-finished blueprint moved from the per-room
# `anyvac_clean_finished` to `anyvac_run_finished` (docs/52 R6) — the old one
# would now notify after every single room.
_OUTDATED_BLUEPRINTS: dict[str, frozenset[str]] = {
    "anyvac_clean_finished.yaml": frozenset(
        {"c5d3560abaae6cf6378e1cba4c20c048a75a93f76c44c50af70f4951570e17b3"}
    ),
}


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _install_blueprints(hass: HomeAssistant) -> None:
    """Copy bundled notification blueprints into config/blueprints/automation/anyvac/.

    Never overwrites a blueprint the user has edited: an existing file is only
    replaced when it is byte-for-byte an earlier bundled version.
    """
    src_dir = os.path.join(os.path.dirname(__file__), "blueprints")
    if not os.path.isdir(src_dir):
        return
    dst_dir = hass.config.path("blueprints", "automation", "anyvac")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        for fn in os.listdir(src_dir):
            if not fn.endswith(".yaml"):
                continue
            dst = os.path.join(dst_dir, fn)
            src = os.path.join(src_dir, fn)
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            elif _sha256(dst) in _OUTDATED_BLUEPRINTS.get(fn, ()):
                # An earlier bundled version nobody edited: replace it.
                shutil.copy(src, dst)
                _LOGGER.info("AnyVac: updated blueprint %s", fn)
    except OSError as err:
        _LOGGER.warning("AnyVac: could not install notification blueprints: %s", err)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on an options change (1.1.0) — the coordinator reads its options
    once at construction, so a reload is what applies them."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AnyVac from a config entry."""
    await hass.async_add_executor_job(_install_blueprints, hass)
    coordinator = AnyVacCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # docs/47: the coordinator listens on the Roborock coordinators; drop those
    # listeners on unload (a reload creates a fresh coordinator that re-subscribes).
    entry.async_on_unload(coordinator.unsubscribe_roborock)
    # docs/48: live position from the robot's local diff while it cleans.
    entry.async_on_unload(coordinator.start_live())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    # docs/52 §9.5 (R3): a job that ran when Home Assistant stopped goes on.
    entry.async_create_background_task(
        hass, async_restore_job(hass, coordinator), "anyvac_restore_job"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AnyVac config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # 1.51.8 (plan po auditu, B2): an unloaded integration must not keep
        # dispatching robots. A running job's listeners used to outlive the
        # entry — after a disable or removal they still sent `app_segment_clean`
        # on the next `anyvac_room_done`. The robots finish what they were
        # already doing; only the orchestration stops.
        # The saved job stays: a reload (or the next start) resumes it.
        started = _cancel_jobs(hass, keep_saved=True)
        if started:
            _LOGGER.warning(
                "AnyVac unloaded during a clean job; the remaining queued rooms "
                "were not sent (%s)",
                ", ".join(sorted(started)),
            )
        for svc in ALL_SERVICES:
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete AnyVac's `.storage` files when its last entry is removed (B2).

    Room history, estimates, pins, coverage and the home frame are per
    installation, not per entry, so they stay while another AnyVac entry
    exists. Blueprints are left alone: automations may still use them."""
    if any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    for key in STORE_KEYS:
        await Store(hass, 1, f"{DOMAIN}_{key}").async_remove()
