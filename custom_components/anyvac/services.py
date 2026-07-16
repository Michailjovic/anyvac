"""AnyVac services (kontrakt v2, docs/14 §5).

Public command interface for the card (and automations):

- ``anyvac.clean``      — clean intent: rooms + mode (+ vacuums / pin / settings).
                          The backend plans (capability, LPT, pinning), builds the
                          gated task list and executes it server-side.
- ``anyvac.plan``       — the same planner, response-only (assignment preview).
- ``anyvac.goto``       — pin & go; click as PERCENT of the map image, mm math here.
- ``anyvac.zone_clean`` — zone clean; corners as percent, mm math here.
- ``anyvac.cancel``     — tear down running jobs and send the started robots home.
- ``anyvac.select_rooms`` / ``anyvac.pin_room`` / ``anyvac.set_layers`` /
  ``anyvac.set_room_sequence`` / ``anyvac.reset_learning`` — state.
- ``anyvac.run_job``    — INTERNAL executor (docs/14 §5: undocumented); kept
                          registered for the transition period while the card still
                          builds v1 plans, removed from docs in Fáze 3.

Execution model (proven in the field by the card-built v1 plans): a job is a list
of tasks; a task with no ``after`` runs immediately, the rest run when all their
``anyvac_room_done`` / ``anyvac_clean_finished`` conditions have fired. Starting a
new job cancels the previous one (docs/13 C6 — no double-driving robots).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .planner import CleanPlanner, duid_for_entity, vacuum_entity_for_duid

_LOGGER = logging.getLogger(__name__)

SERVICE_RUN_JOB = "run_job"
SERVICE_SELECT_ROOMS = "select_rooms"
SERVICE_PIN_ROOM = "pin_room"
SERVICE_SET_LAYERS = "set_layers"
SERVICE_SET_ROOM_SEQUENCE = "set_room_sequence"
SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_CLEAN = "clean"
SERVICE_PLAN = "plan"
SERVICE_GOTO = "goto"
SERVICE_ZONE_CLEAN = "zone_clean"
SERVICE_CANCEL = "cancel"

ALL_SERVICES = (
    SERVICE_RUN_JOB,
    SERVICE_SELECT_ROOMS,
    SERVICE_PIN_ROOM,
    SERVICE_SET_LAYERS,
    SERVICE_SET_ROOM_SEQUENCE,
    SERVICE_RESET_LEARNING,
    SERVICE_CLEAN,
    SERVICE_PLAN,
    SERVICE_GOTO,
    SERVICE_ZONE_CLEAN,
    SERVICE_CANCEL,
)

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
PIN_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("room"): str,
        # Vacuum entity_id or duid; omitted/empty = unpin the room.
        vol.Optional("vacuum"): vol.Any(str, None),
    }
)
SET_LAYERS_SCHEMA = vol.Schema(
    {
        vol.Optional("dry"): bool,
        vol.Optional("wet"): bool,
    }
)
SET_ROOM_SEQUENCE_SCHEMA = vol.Schema(
    {
        # Full ordered room-name list (position = 1-based sequence number). The
        # editor always sends its complete known room list on reorder — this
        # replaces the whole stored sequence, it does not merge.
        vol.Required("rooms"): [str],
    }
)
RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Optional("duid"): str,
        vol.Optional("room"): str,
        vol.Optional("kind"): vol.In(["dry", "wet"]),
        vol.Optional("estimates", default=True): bool,
        vol.Optional("baselines", default=True): bool,
    }
)

_SETTINGS_KIND_SCHEMA = vol.Schema(
    {
        vol.Optional("fan_speed"): str,
        vol.Optional("mop_mode"): str,
        vol.Optional("mop_intensity"): str,
        vol.Optional("repeat"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    }
)
CLEAN_SCHEMA = vol.Schema(
    {
        vol.Required("rooms"): [str],
        vol.Optional("mode", default="dry"): vol.In(["dry", "wet", "both"]),
        vol.Optional("vacuums"): vol.Any(
            [str],
            vol.Schema({vol.Optional("dry"): [str], vol.Optional("wet"): [str]}),
        ),
        vol.Optional("pin"): {str: str},
        vol.Optional("settings"): vol.Schema(
            {
                vol.Optional("dry"): _SETTINGS_KIND_SCHEMA,
                vol.Optional("wet"): _SETTINGS_KIND_SCHEMA,
            }
        ),
    }
)
GOTO_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Required("x_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("y_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
    }
)
ZONE_CLEAN_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Required("x1_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("y1_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("x2_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("y2_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    }
)
CANCEL_SCHEMA = vol.Schema({vol.Optional("return_to_base", default=True): bool})


def _coordinators(hass: HomeAssistant) -> list[Any]:
    """All AnyVac coordinators (in practice one config entry)."""
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = getattr(entry, "runtime_data", None)
        if coord is not None:
            out.append(coord)
    return out


def _active_jobs(hass: HomeAssistant) -> list[_JobRunner]:
    return hass.data.setdefault(DOMAIN, {}).setdefault("jobs", [])


def _cancel_jobs(hass: HomeAssistant) -> set[str]:
    """Tear down all running jobs; returns the vacuums whose tasks were started."""
    started: set[str] = set()
    for job in list(_active_jobs(hass)):
        started |= job.started_vacuums
        job.finish()
    return started


class _JobRunner:
    """Executes a plan of gated vacuum tasks, server-side."""

    def __init__(
        self,
        hass: HomeAssistant,
        tasks: list[dict[str, Any]],
        job_rooms: dict[str, set[str]] | None = None,
    ) -> None:
        self.hass = hass
        self.tasks: dict[str, dict[str, Any]] = {
            str(t.get("id", i)): t for i, t in enumerate(tasks)
        }
        self.pending: set[str] = set(self.tasks)
        self.done: set[tuple[Any, Any]] = set()
        # Vacuums whose clean command was actually dispatched — the set anyvac.cancel
        # sends home (a never-started robot has nothing to return from).
        self.started_vacuums: set[str] = set()
        # Plan-scope transit labeling (docs/17 §1.3): {duid: room-name scope}, applied
        # to every known coordinator on start() and cleared on finish() — only
        # anyvac.clean's planned jobs carry this (run_job's raw task lists don't know
        # room scope, and docs/17 explicitly says not to bolt it on there separately).
        self.job_rooms: dict[str, set[str]] = job_rooms or {}
        self._unsub: list = []
        self._cancel_timeout = None

    async def start(self) -> None:
        _active_jobs(self.hass).append(self)
        for duid, rooms in self.job_rooms.items():
            for coord in _coordinators(self.hass):
                coord.set_job_rooms(duid, rooms)
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
            self.finish()

    async def _run_task(self, task: dict[str, Any]) -> None:
        # Pre-clean settings are best-effort: an unknown select option must not
        # abort the clean command itself.
        for sel in task.get("selects") or []:
            try:
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": sel["entity_id"], "option": sel["option"]},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "AnyVac run_job: select %s -> %s failed: %s",
                    sel.get("entity_id"),
                    sel.get("option"),
                    err,
                )
        if task.get("fan_speed") and task.get("vacuum"):
            try:
                await self.hass.services.async_call(
                    "vacuum",
                    "set_fan_speed",
                    {"entity_id": task["vacuum"], "fan_speed": task["fan_speed"]},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("AnyVac run_job: set_fan_speed failed: %s", err)
        service = task.get("service")
        if service and "." in service:
            domain, name = service.split(".", 1)
            await self.hass.services.async_call(
                domain, name, dict(task.get("service_data") or {}), blocking=True
            )
            if task.get("vacuum"):
                self.started_vacuums.add(task["vacuum"])

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
        self.finish()

    def finish(self) -> None:
        """Stop listening and deregister (idempotent)."""
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None
        # Clear this job's plan-scope (docs/17 §1.3) on every path that ends a job —
        # completion, cancellation, and the timeout safety net all funnel through here.
        for duid in self.job_rooms:
            for coord in _coordinators(self.hass):
                coord.set_job_rooms(duid, None)
        jobs = _active_jobs(self.hass)
        if self in jobs:
            jobs.remove(self)


async def _start_job(
    hass: HomeAssistant,
    tasks: list[dict[str, Any]],
    job_rooms: dict[str, set[str]] | None = None,
) -> None:
    """Cancel any previous job (docs/13 C6: no parallel double-driving) and run."""
    _cancel_jobs(hass)
    runner = _JobRunner(hass, tasks, job_rooms)
    await runner.start()


def _resolve_target_duid(hass: HomeAssistant, call: ServiceCall) -> str:
    duid = call.data.get("duid")
    if not duid and call.data.get("entity_id"):
        duid = duid_for_entity(hass, call.data["entity_id"])
    if not duid:
        raise HomeAssistantError(
            "anyvac: provide either 'duid' or 'entity_id' of the target vacuum"
        )
    return duid


def _mm_for(hass: HomeAssistant, duid: str, x_pct: float, y_pct: float) -> tuple[int, int]:
    for coord in _coordinators(hass):
        mm = coord.pct_to_mm(duid, x_pct, y_pct)
        if mm is not None:
            return mm
    raise HomeAssistantError(
        f"anyvac: no map/calibration available for vacuum '{duid}' — cannot convert "
        "map percentages to coordinates"
    )


def async_register_services(hass: HomeAssistant) -> None:  # noqa: C901 - one registrar
    """Register the AnyVac services (idempotent)."""

    async def _handle_run_job(call: ServiceCall) -> None:
        await _start_job(hass, list(call.data["tasks"]))

    async def _handle_select_rooms(call: ServiceCall) -> None:
        rooms = list(call.data.get("rooms", []))
        mode = call.data.get("mode", "set")
        for coord in _coordinators(hass):
            coord.set_selection(rooms, mode)

    async def _handle_pin_room(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.set_room_pin(call.data["room"], call.data.get("vacuum"))

    async def _handle_set_layers(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.set_layers(call.data.get("dry"), call.data.get("wet"))

    async def _handle_set_room_sequence(call: ServiceCall) -> None:
        rooms = [str(r) for r in call.data.get("rooms", [])]
        for coord in _coordinators(hass):
            coord.set_room_sequence(rooms)

    async def _handle_reset_learning(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.reset_learning(
                duid=call.data.get("duid"),
                room=call.data.get("room"),
                kind=call.data.get("kind"),
                estimates=call.data.get("estimates", True),
                baselines=call.data.get("baselines", True),
            )

    def _job_rooms_from_plan(plan: dict[str, Any]) -> dict[str, set[str]]:
        """Plan-scope transit labeling (docs/17 §1.3): per-duid room scope for the
        job about to run — the union of a vacuum's dry + wet assignment, so a
        both-capable robot's wet-only rooms aren't flagged transit during its dry
        pass and vice versa. `plan["dry"]`/`plan["wet"]` are keyed by entity_id
        (the planner's own output shape); resolved to duid here since that's what
        the coordinator's per-poll pipeline keys everything by."""
        by_entity: dict[str, set[str]] = {}
        for kind in ("dry", "wet"):
            for entity, rooms in (plan.get(kind) or {}).items():
                by_entity.setdefault(entity, set()).update(rooms)
        out: dict[str, set[str]] = {}
        for entity, rooms in by_entity.items():
            duid = duid_for_entity(hass, entity)
            if duid:
                out[duid] = rooms
        return out

    def _build(call: ServiceCall) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        coords = _coordinators(hass)
        if not coords:
            raise HomeAssistantError("anyvac: integration is not set up")
        planner = CleanPlanner(hass, coords[0])
        # Stored per-room pins (anyvac.pin_room, docs/18 §7e) are the default;
        # an explicit `pin` parameter on the call wins outright.
        pin = call.data.get("pin")
        if not pin:
            pin = coords[0].room_pins or None
        tasks, plan = planner.build_tasks(
            rooms=[str(r) for r in call.data["rooms"]],
            mode=call.data.get("mode", "dry"),
            vacuums=call.data.get("vacuums"),
            pin=pin,
            settings=call.data.get("settings"),
        )
        return tasks, plan

    async def _handle_clean(call: ServiceCall) -> None:
        tasks, plan = _build(call)
        if not tasks:
            raise HomeAssistantError(
                "anyvac.clean: no capable vacuum found for the requested rooms/mode "
                f"(plan: {plan})"
            )
        _LOGGER.info("AnyVac clean: %s", plan)
        await _start_job(hass, tasks, _job_rooms_from_plan(plan))

    async def _handle_plan(call: ServiceCall) -> dict[str, Any]:
        tasks, plan = _build(call)
        return {"plan": plan, "tasks": tasks}

    async def _handle_goto(call: ServiceCall) -> None:
        duid = _resolve_target_duid(hass, call)
        x, y = _mm_for(hass, duid, call.data["x_pct"], call.data["y_pct"])
        entity = call.data.get("entity_id") or vacuum_entity_for_duid(hass, duid)
        if not entity:
            raise HomeAssistantError(f"anyvac.goto: no vacuum entity for duid '{duid}'")
        await hass.services.async_call(
            "vacuum",
            "send_command",
            {"entity_id": entity, "command": "app_goto_target", "params": [x, y]},
            blocking=True,
        )

    async def _handle_zone_clean(call: ServiceCall) -> None:
        duid = _resolve_target_duid(hass, call)
        ax, ay = _mm_for(hass, duid, call.data["x1_pct"], call.data["y1_pct"])
        bx, by = _mm_for(hass, duid, call.data["x2_pct"], call.data["y2_pct"])
        entity = call.data.get("entity_id") or vacuum_entity_for_duid(hass, duid)
        if not entity:
            raise HomeAssistantError(
                f"anyvac.zone_clean: no vacuum entity for duid '{duid}'"
            )
        zone = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), call.data["repeat"]]
        await hass.services.async_call(
            "vacuum",
            "send_command",
            {"entity_id": entity, "command": "app_zoned_clean", "params": [zone]},
            blocking=True,
        )

    async def _handle_cancel(call: ServiceCall) -> None:
        started = _cancel_jobs(hass)
        if call.data.get("return_to_base", True) and started:
            await hass.services.async_call(
                "vacuum",
                "return_to_base",
                {"entity_id": sorted(started)},
                blocking=False,
            )

    registrations: list[tuple[str, Any, vol.Schema, SupportsResponse]] = [
        (SERVICE_RUN_JOB, _handle_run_job, RUN_JOB_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SELECT_ROOMS, _handle_select_rooms, SELECT_ROOMS_SCHEMA, SupportsResponse.NONE),
        (SERVICE_PIN_ROOM, _handle_pin_room, PIN_ROOM_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SET_LAYERS, _handle_set_layers, SET_LAYERS_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SET_ROOM_SEQUENCE, _handle_set_room_sequence, SET_ROOM_SEQUENCE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_RESET_LEARNING, _handle_reset_learning, RESET_LEARNING_SCHEMA, SupportsResponse.NONE),
        (SERVICE_CLEAN, _handle_clean, CLEAN_SCHEMA, SupportsResponse.NONE),
        (SERVICE_PLAN, _handle_plan, CLEAN_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_GOTO, _handle_goto, GOTO_SCHEMA, SupportsResponse.NONE),
        (SERVICE_ZONE_CLEAN, _handle_zone_clean, ZONE_CLEAN_SCHEMA, SupportsResponse.NONE),
        (SERVICE_CANCEL, _handle_cancel, CANCEL_SCHEMA, SupportsResponse.NONE),
    ]
    for name, handler, schema, supports in registrations:
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN, name, handler, schema=schema, supports_response=supports
            )
