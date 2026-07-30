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
import os
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

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
SERVICE_DOCK_EMPTY = "dock_empty"
SERVICE_DOCK_WASH = "dock_wash"
SERVICE_DOCK_DRY = "dock_dry"
SERVICE_DOCK_PUMP = "dock_pump"
SERVICE_DOCK_SELF_CLEAN = "dock_self_clean"
SERVICE_SNAPSHOT_FLOORPLAN = "snapshot_map_as_floorplan"

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
    SERVICE_DOCK_EMPTY,
    SERVICE_DOCK_WASH,
    SERVICE_DOCK_DRY,
    SERVICE_DOCK_PUMP,
    SERVICE_DOCK_SELF_CLEAN,
    SERVICE_SNAPSHOT_FLOORPLAN,
)

JOB_TIMEOUT_SECONDS = 3 * 3600  # safety: tear down a stuck job after 3 h

# docs/23: adaptive threshold batching for a pool task's progressive dispatch.
# If another of the robot's rooms is estimated ready within this many minutes,
# wait for it (one dock trip covers both) instead of dispatching immediately
# with just what's ready now. Never learned/tuned from real data (unlike the
# per-room time estimates, docs/16) — a fixed starting point, revisit from
# field experience.
BATCH_WAIT_THRESHOLD_MIN = 3.0
# Hard cap on how long a ready batch can be held back waiting for a "soon"
# room, measured from the moment it FIRST had something ready to send — a bad
# estimate must never stall a batch indefinitely.
BATCH_WAIT_CAP_MIN = 6.0

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
        # "dry" or "wet" — dry/wet pins are independent (2026-07-25). Omitted
        # together with "vacuum" unpins the room entirely (both passes);
        # given with no/empty "vacuum" unpins just that one pass.
        vol.Optional("kind"): vol.In(["dry", "wet"]),
        # Vacuum entity_id or duid; omitted/empty = unpin (see "kind").
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
        # Per-room, per-pass override — same shape as the coordinator's stored
        # room_pins (2026-07-25): {room: {"dry"/"wet": vacuum entity_id/duid}}.
        # A room omitted here falls back to the stored pin, then to automatic
        # assignment.
        vol.Optional("pin"): {str: vol.Schema({vol.Optional("dry"): str, vol.Optional("wet"): str})},
        # Per-vacuum, per-pass settings — {"dry"/"wet": {vacuum entity_id/duid:
        # {fan_speed, mop_mode, mop_intensity, repeat}}} (2026-07-26). Each
        # vacuum doing a pass keeps its own preset; a vacuum with no entry for
        # a pass it's assigned to just runs that pass with firmware defaults.
        vol.Optional("settings"): vol.Schema(
            {
                vol.Optional("dry"): {str: _SETTINGS_KIND_SCHEMA},
                vol.Optional("wet"): {str: _SETTINGS_KIND_SCHEMA},
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
# docs/25 §7 field follow-up (2026-07-24): dock sheet actions (empty/wash/dry).
# All three are dock-only self-maintenance commands — no coordinates, same target
# resolution as goto/zone_clean (entity_id or duid).
DOCK_ACTION_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
    }
)
# docs/30 §4a field follow-up (2026-07-30): merged mode's per-vacuum auto-seat
# fit is hard-disabled without a shared floorplan image (`_editorSeat`/
# `_effectiveSeat` both bail to manual sliders when `image_base.src` is
# unset) — but getting a usable floorplan photo today meant manually saving
# a map image out of HA and re-uploading it into `config/www/`, real friction
# reported live during a new-user onboarding walkthrough. This service lets
# the card do that in one click: snapshot the CARD-RESOLVED map image entity
# (passed in explicitly — the card already resolved which one via
# `_mapEntityFor`, e.g. picking the live floor of a multi-map vacuum; the
# backend deliberately does not re-resolve this itself, to guarantee the
# snapshot matches exactly what the user was previewing) and save it as a
# static file under `config/www/anyvac/` that `image_base.src` can point at.
SNAPSHOT_FLOORPLAN_SCHEMA = vol.Schema(
    {
        vol.Required("image_entity"): str,
        vol.Optional("name"): str,
    }
)

_FLOORPLAN_EXT_FOR_CONTENT_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _floorplan_filename(name: str, content_type: str | None) -> str:
    """Pure helper (no HA/IO) so the naming logic is unit-testable without
    mocking the image platform: slugifies `name` and picks a file extension
    from the fetched image's content type (defaulting to png for anything
    unrecognised — still a valid, openable file even if the guess is wrong)."""
    ext = _FLOORPLAN_EXT_FOR_CONTENT_TYPE.get((content_type or "").lower(), "png")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "vacuum"
    return f"anyvac_floorplan_{slug}.{ext}"


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
        # docs/23: a task carrying a "pool" key is a progressive-dispatch pool
        # task (built by planner.py for a wet-capable robot with 2+ rooms in a
        # "both" job) — it goes through `_dispatch_pools()`, not the plain
        # static all-or-nothing `after`-gated path below.
        self.tasks: dict[str, dict[str, Any]] = {}
        self.pool_tasks: dict[str, dict[str, Any]] = {}
        for i, t in enumerate(tasks):
            tid = str(t.get("id", i))
            if "pool" in t:
                pt = dict(t)
                pt["dispatched"] = set()
                self.pool_tasks[tid] = pt
            else:
                self.tasks[tid] = t
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
        # docs/23: progressive pool-task dispatch state. `pool_busy` maps a duid
        # to the pool task id it's currently running a batch for (absent = free).
        # `_pool_first_ready` marks when a task's ready-set first became
        # non-empty, so the wait-vs-go decision's cap (BATCH_WAIT_CAP_MIN) has a
        # fixed reference point instead of restarting the clock on every event.
        self.pool_busy: dict[str, str] = {}
        self._pool_first_ready: dict[str, Any] = {}
        self._pool_wait_cancel: dict[str, Any] = {}
        self._start_time: Any = None

    async def start(self) -> None:
        _active_jobs(self.hass).append(self)
        self._start_time = dt_util.utcnow()
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
        await self._dispatch_pools()
        self._maybe_finish()

    def _met(self, task: dict[str, Any]) -> bool:
        for cond in task.get("after") or []:
            if (cond.get("duid"), cond.get("room")) not in self.done:
                return False
        return True

    def _maybe_finish(self) -> None:
        if not self.pending and not self.pool_tasks:
            self.finish()

    async def _dispatch_ready(self) -> None:
        for tid in list(self.pending):
            if self._met(self.tasks[tid]):
                self.pending.discard(tid)  # discard before await: no double dispatch
                try:
                    await self._run_task(self.tasks[tid])
                except Exception as err:  # noqa: BLE001 - one bad task must not wedge the job
                    _LOGGER.warning("AnyVac run_job: task %s failed: %s", tid, err)

    # -- docs/23: progressive pool-task dispatch ---------------------------------

    async def _dispatch_pools(self) -> None:
        """Advance every pool task by one step: dispatch a batch if one is due,
        or (re-)schedule a short wait for a room that's estimated to be ready
        soon (§3 adaptive threshold — see docs/23). Safe to call repeatedly;
        each call re-evaluates from scratch (any previously scheduled recheck
        for a task is cancelled and replaced or resolved into a dispatch)."""
        for tid, pt in list(self.pool_tasks.items()):
            dispatched: set[str] = pt["dispatched"]
            remaining = {r: m for r, m in pt["pool"].items() if r not in dispatched}
            if not remaining:
                continue  # fully dispatched; closes out via _on_finished
            duid = pt["duid"]
            if self.pool_busy.get(duid):
                continue  # robot mid-batch (this task or another pool task)
            own_gate = pt.get("own_gate")
            if own_gate and (own_gate.get("duid"), own_gate.get("room")) not in self.done:
                continue  # both-capable robot: own dry session not done yet
            ready = [
                r for r, m in remaining.items()
                if (m["gate"].get("duid"), m["gate"].get("room")) in self.done
            ]
            cancel = self._pool_wait_cancel.pop(tid, None)
            if cancel is not None:
                cancel()  # re-deciding fresh below, whatever it was waiting for is moot
            if not ready:
                continue  # nothing ready yet; the next event re-enters this method
            if tid not in self._pool_first_ready:
                self._pool_first_ready[tid] = dt_util.utcnow()
            not_ready = [r for r in remaining if r not in ready]
            elapsed_min = (dt_util.utcnow() - self._pool_first_ready[tid]).total_seconds() / 60
            if not_ready and elapsed_min < BATCH_WAIT_CAP_MIN:
                soonest = min(remaining[r]["eta_min"] for r in not_ready)
                now_min = (dt_util.utcnow() - self._start_time).total_seconds() / 60
                wait_needed = soonest - now_min
                if 0 < wait_needed <= BATCH_WAIT_THRESHOLD_MIN:
                    # docs/23 field test observability (2026-07-25): the wait-vs-go
                    # decision itself had no log trail — worth seeing which room(s)
                    # a batch waited for and for how long, not just the eventual
                    # send_command.
                    _LOGGER.info(
                        "AnyVac pool %s (%s): %s ready, waiting ~%.1f min for %s "
                        "(elapsed %.1f/%s min cap)",
                        tid, pt["vacuum"], sorted(ready), wait_needed,
                        sorted(not_ready), elapsed_min, BATCH_WAIT_CAP_MIN,
                    )
                    self._pool_wait_cancel[tid] = async_call_later(
                        self.hass, wait_needed * 60, self._make_pool_recheck(tid)
                    )
                    continue
            if not_ready:
                _LOGGER.info(
                    "AnyVac pool %s (%s): dispatching %s now, not waiting for %s "
                    "(elapsed %.1f min%s)",
                    tid, pt["vacuum"], sorted(ready), sorted(not_ready), elapsed_min,
                    " >= cap" if elapsed_min >= BATCH_WAIT_CAP_MIN else " — nothing due soon",
                )
            await self._dispatch_pool_batch(tid, pt, ready)

    def _make_pool_recheck(self, tid: str):
        async def _cb(_now: Any) -> None:
            self._pool_wait_cancel.pop(tid, None)
            await self._dispatch_pools()
            self._maybe_finish()
        return _cb

    async def _dispatch_pool_batch(self, tid: str, pt: dict[str, Any], rooms: list[str]) -> None:
        pt["dispatched"].update(rooms)
        self._pool_first_ready.pop(tid, None)  # reset for this pool's NEXT batch, if any
        entity = pt["vacuum"]
        for sel in pt.get("selects") or []:
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
                    sel.get("entity_id"), sel.get("option"), err,
                )
        if pt.get("fan_speed"):
            try:
                await self.hass.services.async_call(
                    "vacuum",
                    "set_fan_speed",
                    {"entity_id": entity, "fan_speed": pt["fan_speed"]},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("AnyVac run_job: set_fan_speed failed: %s", err)
        segs = [pt["pool"][r]["segment"] for r in rooms]
        try:
            await self.hass.services.async_call(
                "vacuum",
                "send_command",
                {
                    "entity_id": entity,
                    "command": "app_segment_clean",
                    "params": [{"segments": segs, "repeat": pt.get("repeat", 1)}],
                },
                blocking=True,
            )
            _LOGGER.info("AnyVac pool %s (%s): dispatched %s", tid, entity, sorted(rooms))
        except Exception as err:  # noqa: BLE001 - one bad batch must not wedge the job
            _LOGGER.warning(
                "AnyVac run_job: pool task %s batch %s failed: %s", tid, rooms, err
            )
            return  # dispatch itself failed — don't mark the robot busy/started
        self.started_vacuums.add(entity)
        self.pool_busy[pt["duid"]] = tid

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
        await self._dispatch_pools()
        self._maybe_finish()

    async def _on_finished(self, event) -> None:
        # Whole-session completion satisfies a condition with the room omitted.
        duid = event.data.get("duid")
        self.done.add((duid, None))
        # docs/23: if this was a pool task's batch finishing, the robot is free
        # again — clear it and close out the task if that was its last batch.
        tid = self.pool_busy.pop(duid, None)
        if tid is not None:
            pt = self.pool_tasks.get(tid)
            if pt is not None and not (set(pt["pool"]) - pt["dispatched"]):
                self.pool_tasks.pop(tid, None)
        await self._dispatch_ready()
        await self._dispatch_pools()
        self._maybe_finish()

    def _on_timeout(self, _now) -> None:
        if self.pending or self.pool_tasks:
            _LOGGER.warning(
                "AnyVac run_job: %d static + %d pool task(s) never became ready; cleaning up.",
                len(self.pending), len(self.pool_tasks),
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
        for cancel in self._pool_wait_cancel.values():
            cancel()
        self._pool_wait_cancel = {}
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
            coord.set_room_pin(
                call.data["room"], call.data.get("vacuum"), call.data.get("kind")
            )

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

    async def _dock_command(call: ServiceCall, command: str, params: Any = None) -> None:
        """Shared plumbing for the three dock actions below — resolve the target
        vacuum the same way goto/zone_clean do, send a raw dock command (no
        coordinates, no map math)."""
        duid = _resolve_target_duid(hass, call)
        entity = call.data.get("entity_id") or vacuum_entity_for_duid(hass, duid)
        if not entity:
            raise HomeAssistantError(f"anyvac: no vacuum entity for duid '{duid}'")
        data: dict[str, Any] = {"entity_id": entity, "command": command}
        if params is not None:
            data["params"] = params
        await hass.services.async_call("vacuum", "send_command", data, blocking=True)

    async def _handle_dock_empty(call: ServiceCall) -> None:
        # app_start_collect_dust — documented, confirmed command (docs/26 §3).
        await _dock_command(call, "app_start_collect_dust")

    async def _handle_dock_wash(call: ServiceCall) -> None:
        # app_start_wash — documented, confirmed command (docs/26 §3).
        await _dock_command(call, "app_start_wash")

    async def _handle_dock_dry(call: ServiceCall) -> None:
        # app_set_dryer_status — found in python-roborock's RoborockCommand enum
        # (roborock_typing.py) but NOT in its documented command list; no other
        # dedicated "start drying now" command exists there (app_get/set_dryer_setting
        # only configure the scheduled duration). Verified live 2026-07-24 against the
        # user's real S8 MaxV Ultra (Developer Tools → vacuum.send_command, params
        # {"status": 1} then {"status": 0}) — both accepted without error, mirroring
        # the docs/26 verification method (no response payload to inspect either way,
        # same limitation noted there for action/set commands).
        await _dock_command(call, "app_set_dryer_status", {"status": 1})

    async def _handle_dock_pump(call: ServiceCall) -> None:
        # app_empty_rinse_tank_water — found in python-roborock's RoborockCommand
        # enum, not in its documented list. Maps to the manufacturer app's "Pump"
        # action under Dock Maintenance ("drain the remaining water from the
        # cleaning sink") — this is the mop-rinse basin every dock has, distinct
        # from the optional Fill&Drain plumbing accessory's own sewage tank.
        # Verified live 2026-07-24 against the user's real S8 MaxV Ultra
        # (Developer Tools → vacuum.send_command, no params) — pump audibly ran,
        # matched the app's "Pump" action per the user's own comparison.
        await _dock_command(call, "app_empty_rinse_tank_water")

    async def _handle_dock_self_clean(call: ServiceCall) -> None:
        # app_amethyst_self_check — found in python-roborock's RoborockCommand
        # enum ("amethyst" appears to be Roborock's internal codename for the
        # Fill&Drain plumbing accessory). Maps to the manufacturer app's "Dock
        # and Fill&Drain Element Self-Cleaning" action ("flushes the cleaning
        # sink and the built-in sewage tank for the fill&drain element; performs
        # a self-check on the element") — the wording match ("self-check on the
        # element") is what pointed at this command. Verified live 2026-07-24
        # against the user's real S8 MaxV Ultra (Developer Tools →
        # vacuum.send_command, no params) — confirmed by the user as exactly the
        # action they wanted. Only meaningful on vacuums with the Fill&Drain
        # plumbing accessory installed (S8 MaxV Ultra here); shown unconditionally
        # like the other dock actions, matching existing precedent (docs/26 §3 —
        # dock actions aren't gated on detected hardware, since HA/firmware has
        # no documented way to report which dock accessories are installed).
        await _dock_command(call, "app_amethyst_self_check")

    async def _handle_snapshot_floorplan(call: ServiceCall) -> dict[str, Any]:
        entity_id = call.data["image_entity"]
        if hass.states.get(entity_id) is None:
            raise HomeAssistantError(
                f"anyvac.snapshot_map_as_floorplan: entity '{entity_id}' not found"
            )
        try:
            from homeassistant.components.image import async_get_image

            image = await async_get_image(hass, entity_id, timeout=15)
        except Exception as err:  # noqa: BLE001 - surface as one clear service error
            raise HomeAssistantError(
                f"anyvac.snapshot_map_as_floorplan: could not fetch image from "
                f"'{entity_id}': {err}"
            ) from err

        filename = _floorplan_filename(
            call.data.get("name") or entity_id.split(".", 1)[-1], image.content_type
        )
        target_dir = hass.config.path("www", "anyvac")
        target_path = os.path.join(target_dir, filename)

        def _write() -> None:
            os.makedirs(target_dir, exist_ok=True)
            with open(target_path, "wb") as f:
                f.write(image.content)

        try:
            await hass.async_add_executor_job(_write)
        except OSError as err:
            raise HomeAssistantError(
                f"anyvac.snapshot_map_as_floorplan: could not write '{target_path}': {err}"
            ) from err

        # Cache-bust: browsers/HA frontend will happily cache /local/ files by
        # URL, and a re-snapshot (same filename, new bytes) must actually show
        # the new image once set as image_base.src, not a stale cached one.
        url = f"/local/anyvac/{filename}?t={int(time.time())}"
        _LOGGER.info("AnyVac: snapshotted %s -> %s", entity_id, target_path)
        return {"path": url}

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
        (SERVICE_DOCK_EMPTY, _handle_dock_empty, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_WASH, _handle_dock_wash, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_DRY, _handle_dock_dry, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_PUMP, _handle_dock_pump, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_SELF_CLEAN, _handle_dock_self_clean, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SNAPSHOT_FLOORPLAN, _handle_snapshot_floorplan, SNAPSHOT_FLOORPLAN_SCHEMA, SupportsResponse.ONLY),
    ]
    for name, handler, schema, supports in registrations:
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN, name, handler, schema=schema, supports_response=supports
            )
