"""AnyVac orchestration service: ``anyvac.run_job``.

The card builds an orchestration PLAN (it has the config, capabilities and per-room
estimates) and hands it to this service as a list of tasks. The service executes the
plan SERVER-SIDE — so it survives the dashboard being closed — and gates each task on
the real-time ``anyvac_room_done`` / ``anyvac_clean_finished`` signals. That is how a
wet robot is released to follow a dry robot per room without colliding.

A task::

    {
        "id": "t2",                                  # optional, defaults to index
        "vacuum": "vacuum.s8_maxv",                  # entity used for set_fan_speed
        "selects": [{"entity_id": "select.x", "option": "off"}],  # pre-clean selects (mop)
        "fan_speed": "max",                          # optional set_fan_speed before
        "service": "vacuum.clean_area",              # the clean command to call
        "service_data": {"entity_id": "...", "cleaning_area_id": [...]},
        "after": [{"duid": "...", "room": "Kitchen"}]  # wait for these (room omitted = whole session)
    }

Tasks with no ``after`` run immediately; the rest run when every one of their
conditions has been satisfied by an event.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_RUN_JOB = "run_job"
SERVICE_SELECT_ROOMS = "select_rooms"
SERVICE_SET_LAYERS = "set_layers"
JOB_TIMEOUT_SECONDS = 3 * 3600  # safety: tear down a stuck job after 3 h

RUN_JOB_SCHEMA = vol.Schema({vol.Required("tasks"): [dict]})
SELECT_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional("rooms", default=list): [str],
        vol.Optional("mode", default="set"): vol.In(
            ["set", "add", "remove", "toggle", "clear"]
        ),
    }
)
SET_LAYERS_SCHEMA = vol.Schema(
    {
        vol.Optional("dry"): bool,
        vol.Optional("wet"): bool,
    }
)
SERVICE_RESET_LEARNING = "reset_learning"
RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Optional("duid"): str,
        vol.Optional("room"): str,
        vol.Optional("kind"): vol.In(["dry", "wet"]),
        vol.Optional("estimates", default=True): bool,
        vol.Optional("baselines", default=True): bool,
    }
)


class _JobRunner:
    """Executes a plan of gated vacuum tasks, server-side."""

    def __init__(self, hass: HomeAssistant, tasks: list[dict[str, Any]]) -> None:
        self.hass = hass
        self.tasks: dict[str, dict[str, Any]] = {
            str(t.get("id", i)): t for i, t in enumerate(tasks)
        }
        self.pending: set[str] = set(self.tasks)
        self.done: set[tuple[Any, Any]] = set()
        self._unsub: list = []
        self._cancel_timeout = None

    async def start(self) -> None:
        self._unsub.append(
            self.hass.bus.async_listen(f"{DOMAIN}_room_done", self._on_room_done)
        )
        self._unsub.append(
            self.hass.bus.async_listen(f"{DOMAIN}_clean_finished", self._on_finished)
        )
        self._cancel_timeout = async_call_later(
            self.hass, JOB_TIMEOUT_SECONDS, self._on_timeout
        )
        await self._dispatch_ready()

    def _met(self, task: dict[str, Any]) -> bool:
        for cond in task.get("after") or []:
            if (cond.get("duid"), cond.get("room")) not in self.done:
                return False
        return True

    async def _dispatch_ready(self) -> None:
        for tid in list(self.pending):
            if self._met(self.tasks[tid]):
                self.pending.discard(tid)  # discard before await: no double dispatch
                try:
                    await self._run_task(self.tasks[tid])
                except Exception as err:  # noqa: BLE001 - one bad task must not wedge the job
                    _LOGGER.warning("AnyVac run_job: task %s failed: %s", tid, err)
        if not self.pending:
            self._finish()

    async def _run_task(self, task: dict[str, Any]) -> None:
        for sel in task.get("selects") or []:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": sel["entity_id"], "option": sel["option"]},
                blocking=True,
            )
        if task.get("fan_speed") and task.get("vacuum"):
            await self.hass.services.async_call(
                "vacuum",
                "set_fan_speed",
                {"entity_id": task["vacuum"], "fan_speed": task["fan_speed"]},
                blocking=True,
            )
        service = task.get("service")
        if service and "." in service:
            domain, name = service.split(".", 1)
            await self.hass.services.async_call(
                domain, name, dict(task.get("service_data") or {}), blocking=True
            )

    async def _on_room_done(self, event) -> None:
        self.done.add((event.data.get("duid"), event.data.get("room")))
        await self._dispatch_ready()

    async def _on_finished(self, event) -> None:
        # Whole-session completion satisfies a condition with the room omitted.
        self.done.add((event.data.get("duid"), None))
        await self._dispatch_ready()

    def _on_timeout(self, _now) -> None:
        if self.pending:
            _LOGGER.warning(
                "AnyVac run_job: %d task(s) never became ready; cleaning up.",
                len(self.pending),
            )
        self._finish()

    def _finish(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None


def async_register_services(hass: HomeAssistant) -> None:
    """Register the AnyVac services (idempotent)."""
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_JOB):

        async def _handle_run_job(call: ServiceCall) -> None:
            runner = _JobRunner(hass, list(call.data["tasks"]))
            await runner.start()

        hass.services.async_register(
            DOMAIN, SERVICE_RUN_JOB, _handle_run_job, schema=RUN_JOB_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SELECT_ROOMS):

        async def _handle_select_rooms(call: ServiceCall) -> None:
            rooms = list(call.data.get("rooms", []))
            mode = call.data.get("mode", "set")
            for entry in hass.config_entries.async_entries(DOMAIN):
                coord = getattr(entry, "runtime_data", None)
                if coord is not None:
                    coord.set_selection(rooms, mode)

        hass.services.async_register(
            DOMAIN, SERVICE_SELECT_ROOMS, _handle_select_rooms, schema=SELECT_ROOMS_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_LAYERS):

        async def _handle_set_layers(call: ServiceCall) -> None:
            for entry in hass.config_entries.async_entries(DOMAIN):
                coord = getattr(entry, "runtime_data", None)
                if coord is not None:
                    coord.set_layers(call.data.get("dry"), call.data.get("wet"))

        hass.services.async_register(
            DOMAIN, SERVICE_SET_LAYERS, _handle_set_layers, schema=SET_LAYERS_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_LEARNING):

        async def _handle_reset_learning(call: ServiceCall) -> None:
            for entry in hass.config_entries.async_entries(DOMAIN):
                coord = getattr(entry, "runtime_data", None)
                if coord is not None:
                    coord.reset_learning(
                        duid=call.data.get("duid"),
                        room=call.data.get("room"),
                        kind=call.data.get("kind"),
                        estimates=call.data.get("estimates", True),
                        baselines=call.data.get("baselines", True),
                    )

        hass.services.async_register(
            DOMAIN, SERVICE_RESET_LEARNING, _handle_reset_learning, schema=RESET_LEARNING_SCHEMA
        )
