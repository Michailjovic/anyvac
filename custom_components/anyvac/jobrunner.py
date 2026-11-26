"""Room-by-room clean jobs (docs/52).

The firmware ignores a new ``app_segment_clean`` while it cleans, washes the mop
in the middle of a task only, and goes back to its default settings whenever a
task closes (docs/52 §2). So AnyVac keeps the queue itself: every robot gets ONE
room per command, the next one when it finishes the previous (on its way home,
or after it docked to wash the mop), with the settings sent before the command
and again once the robot took it.

A queue item moves ``queued → sent → active → done | blocked | removed``.
The coordinator closes a task and reports what it cleaned
(``anyvac_clean_finished`` with ``rooms_result``); the runner turns that into
the item's result, picks the next item, reports each vacuum once its queue is
done (``anyvac_run_finished``) and the whole job at its end
(``anyvac_job_finished``). The job is saved on every change and resumed after a
Home Assistant restart (docs/52 §9.5).

Batch robots (``batch``, docs/52 R7) get one item per pass with all their
rooms; the firmware then runs them in the order set in the Roborock app.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The robot must take a command within this long (HW: 7–27 s, docs/52 §2).
ACCEPT_TIMEOUT_S = 45
# How often a sent item is checked for acceptance (and the coordinator asked for
# a fresh status) until it is taken.
ACCEPT_CHECK_S = 12
# The firmware resets settings when it takes a task; mop mode shows ~15 s later
# (docs/52 §8.1). Applied again the moment the robot took the room (field
# 2026-11-25: with a 6 s wait S8 vacuumed the start of Corridor at the default
# fan), then checked after VERIFY_DELAY_S and applied once more if the late
# mop mode reset landed.
REAPPLY_DELAY_S = 0
VERIFY_DELAY_S = 20
# Below this the firmware refuses a forced start (docs/52 R2).
MIN_START_BATTERY = 30
# A job that never ends (robot offline, events lost) is torn down after this
# long; also the limit for resuming one after a restart.
JOB_MAX_AGE = timedelta(hours=6)
# A pause before removing the room the robot is in (docs/52 §3.4).
PAUSE_SETTLE_S = 10

ACTIVE_STATES = {
    "cleaning",
    "spot_cleaning",
    "zoned_cleaning",
    "segment_cleaning",
    "robot_status_mopping",
    "clean_mop_cleaning",
    "clean_mop_mopping",
    "segment_mopping",
    "segment_clean_mop_cleaning",
    "zoned_mopping",
    "zoned_clean_mop_cleaning",
    "going_to_wash_the_mop",
}
WASH_STATES = {"washing_the_mop", "washing_the_mop_2"}
# A robot in one of these takes a new task (docs/52 §2.8: also on its way home).
FREE_STATES = {
    "returning_home",
    "docking",
    "charging",
    "charging_complete",
    "idle",
    "charger_disconnected",
    "paused",
}
DOCKED_STATES = {"charging", "charging_complete"}
FINAL = {"done", "blocked", "removed"}


def _active_jobs(hass: HomeAssistant) -> list[JobRunner]:
    return hass.data.setdefault(DOMAIN, {}).setdefault("jobs", [])


def _coordinators(hass: HomeAssistant) -> list[Any]:
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = getattr(entry, "runtime_data", None)
        if coord is not None:
            out.append(coord)
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _parse(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return dt_util.parse_datetime(val)
    return None


class JobRunner:
    """One orchestrated clean job: a queue of rooms per vacuum."""

    def __init__(
        self,
        hass: HomeAssistant,
        items: list[dict[str, Any]],
        *,
        mode: str = "dry",
        settings: dict[str, Any] | None = None,
        context: Context | None = None,
        wash: dict[str, dict[str, Any]] | None = None,
        job_id: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.hass = hass
        # 1.51.8 (B4): the context of the call that started the job, handed to
        # every call the runner makes, so the logbook names the user.
        self.context = context
        self.job_id = job_id or uuid.uuid4().hex[:12]
        self.started_at = started_at or dt_util.utcnow()
        self.mode = mode
        self.settings = settings or {}
        self.warnings: list[str] = []
        self.queues: dict[str, dict[str, Any]] = {}
        self._seq = 0
        for it in items:
            self._add_item(dict(it), wash=(wash or {}).get(it["duid"]))
        # Dry rooms a batch robot reported done (`anyvac_room_done`): the gate of
        # a wet item whose dry pass is one batch with other rooms.
        self.dry_rooms_done: set[str] = set()
        # Vacuums sent a command — what `anyvac.cancel` sends home.
        self.started_vacuums: set[str] = set()
        self._unsub: list[Any] = []
        self._timers: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._finished_reported = False
        self._publishing = False
        self._published: Any = None

    # -- queue helpers -----------------------------------------------------------

    def _add_item(self, it: dict[str, Any], wash: dict[str, Any] | None = None, index: int | None = None) -> dict[str, Any]:
        duid = it["duid"]
        q = self.queues.setdefault(duid, {
            "duid": duid,
            "vacuum": it.get("vacuum"),
            "batch": bool(it.get("batch")),
            "items": [],
            "phase": "idle",
            "wash": dict(wash or {"mode": None, "interval_min": None, "mop_min_since_wash": 0.0}),
            "last_kind": None,
            "reported": False,
            "battery_start": None,
            "first_sent_at": None,
        })
        if wash and not q["wash"].get("mode"):
            q["wash"].update({k: v for k, v in wash.items() if k != "mop_min_since_wash"})
        it.setdefault("state", "queued")
        it.setdefault("manual", False)
        it.setdefault("attempts", 0)
        if it.get("id") is None or any(x["id"] == it["id"] for qq in self.queues.values() for x in qq["items"]):
            self._seq += 1
            it["id"] = f"{it.get('kind')}-{duid}-x{self._seq}"
        items = q["items"]
        if index is None:
            items.append(it)
        else:
            open_items = [x for x in items if x["state"] == "queued"]
            if index >= len(open_items):
                items.append(it)
            else:
                items.insert(items.index(open_items[max(index, 0)]), it)
        return it

    def items(self) -> list[dict[str, Any]]:
        return [it for q in self.queues.values() for it in q["items"]]

    def _current(self, q: dict[str, Any]) -> dict[str, Any] | None:
        return next((it for it in q["items"] if it["state"] in ("sent", "active")), None)

    def _gate_open(self, it: dict[str, Any]) -> bool:
        """A wet item waits for the dry pass of its room(s) (docs/52 §3.1.4)."""
        if it.get("kind") != "wet":
            return True
        for room in it.get("rooms") or []:
            if room in self.dry_rooms_done:
                continue
            for other in self.items():
                if other.get("kind") == "dry" and room in (other.get("rooms") or []) and other["state"] not in FINAL:
                    return False
        return True

    def _occupied(self, q: dict[str, Any]) -> set[str]:
        """Rooms another robot was sent to or is cleaning (docs/53 P4: two
        robots in one room run into each other). Field 2026-11-25: a dry pass
        removed and added again released the wet gate in between, the mop
        robot took the room, and the re-added dry pass would have sent a
        second robot into it."""
        return {
            r
            for other in self.queues.values()
            if other is not q
            for it in other["items"]
            if it["state"] in ("sent", "active")
            for r in it.get("rooms") or []
        }

    def _next_ready(self, q: dict[str, Any]) -> dict[str, Any] | None:
        busy = self._occupied(q)
        for it in q["items"]:
            if it["state"] == "queued" and self._gate_open(it) and not busy & set(it.get("rooms") or []):
                return it
        return None

    def _device_name(self, duid: str) -> str | None:
        for coord in _coordinators(self.hass):
            dev = (getattr(coord, "data", None) or {}).get(duid)
            if dev is not None and getattr(dev, "name", None):
                return dev.name
        return None

    def _device(self, duid: str) -> dict[str, Any]:
        for coord in _coordinators(self.hass):
            dev = (getattr(coord, "data", None) or {}).get(duid)
            if dev is not None:
                return getattr(dev, "data", None) or {}
        return {}

    def _scope(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for duid, q in self.queues.items():
            out[duid] = {r for it in q["items"] if it["state"] != "removed" for r in it.get("rooms") or []}
        return out

    # -- lifecycle -----------------------------------------------------------------

    async def start(self, restored: bool = False) -> None:
        async with self._lock:
            _active_jobs(self.hass).append(self)
            for duid, rooms in self._scope().items():
                for coord in _coordinators(self.hass):
                    coord.set_job_rooms(duid, rooms)
            self._unsub.append(self.hass.bus.async_listen(f"{DOMAIN}_clean_finished", self._on_finished))
            self._unsub.append(self.hass.bus.async_listen(f"{DOMAIN}_room_done", self._on_room_done))
            for coord in _coordinators(self.hass):
                add = getattr(coord, "async_add_listener", None)
                if add is not None:
                    self._unsub.append(add(self._on_coordinator_update))
            self._timers["job"] = async_call_later(self.hass, JOB_MAX_AGE.total_seconds(), self._on_timeout)
            if restored:
                self._reconcile()
            self._publish(force=True)
            await self._step()

    @callback
    def _on_coordinator_update(self) -> None:
        if self._closed or self._publishing:
            return
        self.hass.async_create_task(self._locked_step())

    async def _locked_step(self) -> None:
        async with self._lock:
            if not self._closed:
                await self._step()

    def _schedule(self, key: str, delay: float) -> None:
        old = self._timers.pop(key, None)
        if old is not None:
            old()

        # Must be a HA callback: without it `async_call_later` runs a plain
        # function in a worker thread, `async_create_task` raises there, and the
        # step never runs (field 2026-11-26 — acceptance checks lost, a room
        # blocked as "command not accepted" while the robot was cleaning it).
        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(key, None)
            if not self._closed:
                self.hass.async_create_task(self._locked_step())

        self._timers[key] = async_call_later(self.hass, delay, _fire)

    @callback
    def _on_timeout(self, _now: Any) -> None:
        self._timers.pop("job", None)
        open_items = [it["id"] for it in self.items() if it["state"] not in FINAL]
        _LOGGER.warning("AnyVac job %s: still open after %s (%s); cleaning up.", self.job_id, JOB_MAX_AGE, open_items)
        self.finish()

    # -- the step: send what can be sent ----------------------------------------------

    async def _step(self) -> None:
        now = dt_util.utcnow()
        for duid, q in self.queues.items():
            if self._closed:
                return
            dd = self._device(duid)
            cur = self._current(q)
            if cur is None and dd.get("status_state") in WASH_STATES:
                self.note_washed(duid)  # the dock washed the mop at the end of a task
            if q.get("pausing"):
                await self._continue_after_pause(q, dd, now)
                continue
            if cur is not None:
                if cur["state"] == "sent":
                    await self._check_accepted(q, cur, dd, now)
                else:
                    self._adopt_user_changes(q, cur, dd)
                continue
            if all(it["state"] in FINAL for it in q["items"]):
                q["phase"] = "done"
                self._report_vacuum(q)
                continue
            nxt = self._next_ready(q)
            if nxt is None:
                q["phase"] = "waiting_gate"
                continue
            if not self._robot_free(dd):
                continue
            wait = self._wait_reason(q, nxt, dd)
            if wait:
                q["phase"] = wait
                continue
            await self._send(q, nxt, dd)
        self._publish()
        self._maybe_finish()

    @staticmethod
    def _robot_free(dd: dict[str, Any]) -> bool:
        if not dd:
            return False
        state = dd.get("status_state")
        if not dd.get("in_cleaning"):
            return state not in WASH_STATES and state != "error"
        return state in FREE_STATES

    @staticmethod
    def _docked(dd: dict[str, Any]) -> bool:
        return not dd.get("in_cleaning") or dd.get("status_state") in DOCKED_STATES

    def _wait_reason(self, q: dict[str, Any], it: dict[str, Any], dd: dict[str, Any]) -> str | None:
        """Why the robot should not get `it` right now (docs/52 §3.2, R2)."""
        docked = self._docked(dd)
        if it.get("kind") == "wet" and q.get("last_kind") == "wet" and not q["batch"] and not docked:
            wash = q.get("wash") or {}
            mode = wash.get("mode")
            interval = wash.get("interval_min")
            since = float(wash.get("mop_min_since_wash") or 0.0)
            if mode == "room" or (mode == "unknown") or (mode == "time" and interval and since >= interval):
                return "washing"
        battery = dd.get("battery")
        if docked and isinstance(battery, (int, float)) and battery < MIN_START_BATTERY:
            return "charging"
        return None

    async def _send(self, q: dict[str, Any], it: dict[str, Any], dd: dict[str, Any]) -> None:
        duid = q["duid"]
        entity = q["vacuum"]
        # The map's target segments and the robot's state before it got the
        # command: right after a task ends the map still shows the old room's
        # segments for a while (field 2026-11-26), see `_check_accepted`.
        # Taken before the call — `dd` is the live device data.
        before_targets = sorted(int(x) for x in dd.get("target_segments") or [])
        before_state = dd.get("status_state")
        await self._apply_settings(it)
        for coord in _coordinators(self.hass):
            coord.set_task_item(duid, set(it.get("rooms") or []))
        try:
            await self.hass.services.async_call(
                "vacuum",
                "send_command",
                {
                    "entity_id": entity,
                    "command": "app_segment_clean",
                    "params": [{"segments": list(it["segments"]), "repeat": int(it.get("repeat") or 1)}],
                },
                blocking=True,
                context=self.context,
            )
        except Exception as err:  # noqa: BLE001 - one bad item must not wedge the job
            _LOGGER.warning("AnyVac job %s: sending %s to %s failed: %s", self.job_id, it["rooms"], entity, err)
            it["attempts"] = int(it.get("attempts") or 0) + 1
            if it["attempts"] >= 2:
                self._block(q, it, "command failed")
            else:
                self._schedule(f"retry-{it['id']}", ACCEPT_CHECK_S)
            return
        now = dt_util.utcnow()
        it["state"] = "sent"
        it["sent_at"] = _iso(now)
        it["sent_from_dock"] = self._docked(dd)
        it["sent_targets"] = before_targets
        it["sent_state"] = before_state
        it["attempts"] = int(it.get("attempts") or 0) + 1
        q["phase"] = "starting"
        if q["first_sent_at"] is None:
            q["first_sent_at"] = _iso(now)
            q["battery_start"] = dd.get("battery")
        self.started_vacuums.add(entity)
        _LOGGER.info("AnyVac job %s: %s <- %s (%s, repeat %s)", self.job_id, entity, it["rooms"], it["kind"], it.get("repeat"))
        self._schedule(f"accept-{duid}", ACCEPT_CHECK_S)
        self._save()

    async def _check_accepted(self, q: dict[str, Any], it: dict[str, Any], dd: dict[str, Any], now: datetime) -> None:
        """Did the robot take the command? (docs/52 §9.2.4) The firmware ignores
        a command it cannot take and the service still reports success."""
        state = dd.get("status_state")
        targets = set(dd.get("target_segments") or [])
        # The robot was free when it got the command (the runner sends only
        # then), so a robot that is now cleaning runs OUR task — even while its
        # map still lists the previous room's segments (the map lags the task
        # by up to a snapshot; S8 chained Kitchen -> Living room in 22 s and the
        # map said Kitchen for 45 s). A map showing yet other segments still
        # means a different task.
        stale = (
            it.get("sent_targets") is not None
            and sorted(targets) == it["sent_targets"]
            and it.get("sent_state") not in ACTIVE_STATES
        )
        matches = not targets or targets == set(it["segments"]) or stale
        started = state in ACTIVE_STATES or (
            it.get("sent_from_dock") and dd.get("in_cleaning") and state in WASH_STATES
        )
        if started and matches:
            it["state"] = "active"
            it["accepted_at"] = _iso(now)
            q["phase"] = "cleaning"
            self._save()
            self.hass.async_create_task(self._reapply_and_verify(q["duid"], it))
            return
        sent = _parse(it.get("sent_at")) or now
        if (now - sent).total_seconds() < ACCEPT_TIMEOUT_S:
            for coord in _coordinators(self.hass):
                refresh = getattr(coord, "async_request_refresh", None)
                if refresh is not None:
                    self.hass.async_create_task(refresh())
            self._schedule(f"accept-{q['duid']}", ACCEPT_CHECK_S)
            return
        if int(it.get("attempts") or 0) < 2 and self._robot_free(dd):
            _LOGGER.info("AnyVac job %s: %s did not take %s, sending again", self.job_id, q["vacuum"], it["rooms"])
            it["state"] = "queued"
            await self._send(q, it, dd)
            return
        self._block(q, it, "command not accepted")

    def _block(self, q: dict[str, Any], it: dict[str, Any], reason: str) -> None:
        it["state"] = "blocked"
        it["reason"] = reason
        it["done_at"] = _iso(dt_util.utcnow())
        q["phase"] = "idle"
        for coord in _coordinators(self.hass):
            coord.set_task_item(q["duid"], None)
        _LOGGER.warning("AnyVac job %s: %s %s blocked: %s", self.job_id, q["vacuum"], it["rooms"], reason)
        self._save()
        self.hass.async_create_task(self._locked_step())

    # -- settings ----------------------------------------------------------------------

    async def _apply_settings(self, it: dict[str, Any], record: bool = False) -> list[str]:
        """Selects + fan speed of the item. Returns what did not take."""
        problems: list[str] = []
        states = getattr(self.hass, "states", None)
        for sel in it.get("selects") or []:
            entity, option = sel.get("entity_id"), sel.get("option")
            before = states.get(entity) if states is not None else None
            inert = before is not None and before.state in ("unknown", "unavailable")
            try:
                await self.hass.services.async_call(
                    "select", "select_option", {"entity_id": entity, "option": option},
                    blocking=True, context=self.context,
                )
            except Exception as err:  # noqa: BLE001
                if not inert:
                    problems.append(f"{entity} -> {option} failed: {err}")
        if it.get("fan_speed") and it.get("vacuum"):
            try:
                await self.hass.services.async_call(
                    "vacuum", "set_fan_speed",
                    {"entity_id": it["vacuum"], "fan_speed": it["fan_speed"]},
                    blocking=True, context=self.context,
                )
            except Exception as err:  # noqa: BLE001
                problems.append(f"{it['vacuum']} fan {it['fan_speed']} failed: {err}")
        return problems

    def _settings_mismatch(self, it: dict[str, Any], dd: dict[str, Any]) -> list[str]:
        """What the robot reports differently from the item's settings."""
        out: list[str] = []
        states = getattr(self.hass, "states", None)
        for sel in it.get("selects") or []:
            st = states.get(sel.get("entity_id")) if states is not None else None
            if st is not None and st.state not in ("unknown", "unavailable") and st.state != sel.get("option"):
                out.append(f"{sel['entity_id']} is '{st.state}', expected '{sel.get('option')}'")
        sig = dd.get("mop_signal") or {}
        want = (it.get("expect") or {}).get("fan_speed")
        got = sig.get("fan_speed_name")
        if want and got and str(got).lower() != str(want).lower():
            out.append(f"{it['vacuum']} fan is '{got}', expected '{want}'")
        water = sig.get("water_mode_name")
        if (it.get("expect") or {}).get("water_off") and water and not str(water).lower().startswith("off"):
            # A dry pass of a mop robot: the water must be off (docs/49 A5).
            out.append(f"{it['vacuum']} water is '{water}', expected 'off'")
        return out

    async def _reapply_and_verify(self, duid: str, it: dict[str, Any]) -> None:
        """The firmware resets the settings when it takes a task (S8) or when
        the previous one ends (S7): send them again now, then check (docs/52
        §3.3). Also checks the passes — only while this very item runs, never
        on a stale status (docs/52 O3, the false "reports 1" warning)."""
        await asyncio.sleep(REAPPLY_DELAY_S)
        if self._closed or it["state"] != "active":
            return
        await self._apply_settings(it)
        problems: list[str] = []
        for attempt in range(2):
            await asyncio.sleep(VERIFY_DELAY_S)
            if self._closed or it["state"] != "active":
                return
            dd = self._device(duid)
            problems = self._settings_mismatch(it, dd)
            if not problems:
                break
            if attempt == 0:
                await self._apply_settings(it)
        dd = self._device(duid)
        rep = dd.get("repeat")
        wanted = int(it.get("repeat") or 1)
        if (
            wanted > 1
            and rep is not None
            and dd.get("status_state") in ACTIVE_STATES
            and set(dd.get("target_segments") or []) == set(it["segments"])
            and int(rep) != wanted
        ):
            problems.append(f"{it['vacuum']}: asked for {wanted} passes, the robot reports {rep}")
        for p in problems:
            _LOGGER.warning("AnyVac job %s: %s", self.job_id, p)
            self._warn(p)
        if not self._closed and it["state"] == "active":
            # From now on a change on the robot is the user's (docs/52 §3.3).
            it["baseline"] = self._observe(it, self._device(duid))

    # -- settings the user changes on the robot ----------------------------------------

    def _observe(self, it: dict[str, Any], dd: dict[str, Any]) -> dict[str, Any]:
        """The robot's current fan and mop selects, as far as they are known."""
        out: dict[str, Any] = {}
        fan = (dd.get("mop_signal") or {}).get("fan_speed_name")
        if fan:
            out["fan"] = str(fan)
        states = getattr(self.hass, "states", None)
        ents = set(it.get("watch") or []) | {s.get("entity_id") for s in it.get("selects") or []}
        for ent in sorted(e for e in ents if e):
            st = states.get(ent) if states is not None else None
            if st is not None and st.state not in ("unknown", "unavailable"):
                out[ent] = st.state
        return out

    @staticmethod
    def _set_setting(it: dict[str, Any], key: str, value: str) -> None:
        if key == "fan":
            it["fan_speed"] = value
            it.setdefault("expect", {})["fan_speed"] = value
            return
        sels = it.setdefault("selects", [])
        for sel in sels:
            if sel.get("entity_id") == key:
                sel["option"] = value
                break
        else:
            sels.append({"entity_id": key, "option": value})
        exp = it.setdefault("expect", {})
        if "mop_mode" in key and it.get("kind") == "wet":
            exp["mop_mode"] = value
        if "mop_intensity" in key and it.get("kind") == "dry":
            exp["water_off"] = value == "off"

    def _adopt_user_changes(self, q: dict[str, Any], it: dict[str, Any], dd: dict[str, Any]) -> None:
        """A setting the user changed on the robot while it cleans (the app,
        the card, a button) is kept: for this room and for every room of the
        same pass still in this vacuum's queue. Only while the robot really
        cleans — the firmware's own resets come when a task closes."""
        base = it.get("baseline")
        if base is None or dd.get("status_state") not in ACTIVE_STATES:
            return
        now = self._observe(it, dd)
        changed = {k: v for k, v in now.items() if k in base and base[k] != v}
        if not changed:
            return
        base.update(changed)
        targets = [it] + [x for x in q["items"] if x["state"] == "queued" and x.get("kind") == it.get("kind")]
        for key, value in changed.items():
            for x in targets:
                self._set_setting(x, key, value)
        _LOGGER.info(
            "AnyVac job %s: %s changed on the robot %s — kept for %d room(s)",
            self.job_id, q["vacuum"], changed, len(targets),
        )
        self._save()

    def update_settings(self, duid: str, kind: str, fields: dict[str, Any]) -> list[str]:
        """New settings for one vacuum's pass (`anyvac.edit_job` settings):
        every queued room of it, and the room it cleans right now (applied
        live). `fields` = what `CleanPlanner.make_item` built: selects,
        fan_speed, repeat, expect."""
        q = self.queues.get(duid)
        if q is None:
            return []
        changed: list[str] = []
        for it in q["items"]:
            if it.get("kind") != kind or it["state"] not in ("queued", "sent", "active"):
                continue
            for key in ("selects", "fan_speed", "expect"):
                if key in fields:
                    it[key] = fields[key]
            if it["state"] == "queued" and "repeat" in fields:
                it["repeat"] = fields["repeat"]
            if it["state"] == "active":
                it.pop("baseline", None)
                self.hass.async_create_task(self._reapply_and_verify(duid, it))
            changed.append(it["id"])
        self._save()
        return changed

    def _warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)
        for coord in _coordinators(self.hass):
            coord.add_job_warning(text)

    # -- events ----------------------------------------------------------------------------

    async def _on_room_done(self, event: Any) -> None:
        async with self._lock:
            if self._closed:
                return
            duid = event.data.get("duid")
            room = event.data.get("room")
            q = self.queues.get(duid)
            cur = self._current(q) if q else None
            if cur is not None and cur.get("kind") == "dry" and room in (cur.get("rooms") or []):
                self.dry_rooms_done.add(room)
                await self._step()

    async def _on_finished(self, event: Any) -> None:
        async with self._lock:
            if self._closed:
                return
            await self._handle_finished(event.data)

    async def _handle_finished(self, data: dict[str, Any]) -> None:
        duid = data.get("duid")
        q = self.queues.get(duid)
        if q is None:
            return
        it = self._current(q)
        if it is None:
            self._adopt_unaccepted(q, data)
            return
        if it["state"] == "sent" and not data.get("rooms_result"):
            # The previous task's end, reported after we sent the next one.
            return
        results = {r.get("room"): r for r in data.get("rooms_result") or []}
        rows = [results[r] for r in it.get("rooms") or [] if r in results]
        cleaned = [r for r in rows if r.get("cleaned")]
        now = dt_util.utcnow()
        it["result"] = rows
        it["done_at"] = _iso(now)
        if cleaned:
            it["state"] = "done"
            if it.get("kind") == "dry":
                self.dry_rooms_done.update(r["room"] for r in cleaned)
        else:
            it["state"] = "blocked"
            if data.get("water_shortage"):
                it["reason"] = "water_shortage"
            elif (self._device(duid).get("status_state")) == "error":
                it["reason"] = "error"
            else:
                it["reason"] = "could not reach the room"
            self._warn(f"{q['vacuum']}: {', '.join(it['rooms'])} — {it['reason']}")
        if it.get("kind") == "wet":
            self._count_mopping(q, sum(float(r.get("active_min") or 0) for r in rows))
        q["last_kind"] = it.get("kind")
        q["phase"] = "returning"
        for coord in _coordinators(self.hass):
            coord.set_task_item(duid, None)
        self._save()
        await self._step()

    def _adopt_unaccepted(self, q: dict[str, Any], data: dict[str, Any]) -> None:
        """A room blocked as "command not accepted" that the robot then cleaned
        after all: the task's result says so — take it as done instead of
        leaving the job thinking the room was never cleaned."""
        cleaned = {r.get("room") for r in data.get("rooms_result") or [] if r.get("cleaned")}
        changed = False
        for it in q["items"]:
            if (it["state"] == "blocked" and it.get("reason") == "command not accepted"
                    and set(it.get("rooms") or []) and set(it.get("rooms") or []) <= cleaned):
                it["state"] = "done"
                it.pop("reason", None)
                it["done_at"] = _iso(dt_util.utcnow())
                it["result"] = [r for r in data.get("rooms_result") or [] if r.get("room") in (it.get("rooms") or [])]
                if it.get("kind") == "dry":
                    self.dry_rooms_done.update(it.get("rooms") or [])
                changed = True
                _LOGGER.info("AnyVac job %s: %s %s cleaned after all", self.job_id, q["vacuum"], it["rooms"])
        if changed:
            self._save()
            self.hass.async_create_task(self._locked_step())

    @staticmethod
    def _count_mopping(q: dict[str, Any], minutes: float) -> None:
        """Mopping since the last wash, across tasks (docs/52 §3.2). Inside one
        task the firmware washes on its own interval and starts counting again."""
        wash = q.setdefault("wash", {})
        interval = wash.get("interval_min")
        since = float(wash.get("mop_min_since_wash") or 0.0)
        if interval and minutes >= interval:
            since = minutes % interval
        else:
            since += minutes
        wash["mop_min_since_wash"] = round(since, 1)

    def note_washed(self, duid: str) -> None:
        q = self.queues.get(duid)
        if q is not None:
            q.setdefault("wash", {})["mop_min_since_wash"] = 0.0

    # -- reports ---------------------------------------------------------------------------

    def _report_vacuum(self, q: dict[str, Any]) -> None:
        """`anyvac_run_finished` once per vacuum, after its last item (docs/52 R6)."""
        if q.get("reported") or not any(it["state"] != "removed" for it in q["items"]):
            q["reported"] = True
            return
        q["reported"] = True
        now = dt_util.utcnow()
        started = _parse(q.get("first_sent_at"))
        rooms: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        kinds: set[str] = set()
        for it in q["items"]:
            if it["state"] == "done":
                kinds.add(it["kind"])
                for r in it.get("result") or []:
                    rooms.append({
                        "room": r.get("room"), "kind": it["kind"],
                        "active_min": r.get("active_min"),
                        "completion": r.get(it["kind"]),
                        "passes": r.get("passes"),
                    })
            elif it["state"] == "blocked":
                for room in it.get("rooms") or []:
                    blocked.append({"room": room, "kind": it["kind"], "reason": it.get("reason")})
        dd = self._device(q["duid"])
        bat0, bat1 = q.get("battery_start"), dd.get("battery")
        payload = {
            # Same keys as the coordinator's own `run_finished` (a clean
            # started outside AnyVac), so one automation serves both.
            "vacuum": self._device_name(q["duid"]) or q["vacuum"],
            "entity_id": q["vacuum"],
            "duid": q["duid"],
            "job_id": self.job_id,
            "kind": "both" if len(kinds) == 2 else (next(iter(kinds)) if kinds else None),
            "started_at": q.get("first_sent_at"),
            "finished_at": _iso(now),
            "duration_min": round((now - started).total_seconds() / 60) if started else None,
            "battery_used": (bat0 - bat1) if isinstance(bat0, (int, float)) and isinstance(bat1, (int, float)) else None,
            "rooms": sorted({r["room"] for r in rooms if r.get("room")}),
            "rooms_result": rooms,
            "blocked": blocked,
            "clean_type": "both" if len(kinds) == 2 else (next(iter(kinds)) if kinds else None),
            "kinds": sorted(kinds),
        }
        self.hass.bus.async_fire(f"{DOMAIN}_run_finished", payload)

    def _maybe_finish(self) -> None:
        if self._closed:
            return
        # A robot whose room was blocked as "command not accepted" but which is
        # cleaning anyway may well be doing that room: wait for its task to end
        # (`_adopt_unaccepted`) instead of closing the job under it.
        unsure = [
            q for q in self.queues.values()
            if any(it["state"] == "blocked" and it.get("reason") == "command not accepted" for it in q["items"])
            and self._device(q["duid"]).get("status_state") in ACTIVE_STATES
        ]
        if unsure:
            self._schedule("finish-wait", 60)
            return
        if all(it["state"] in FINAL for it in self.items()) and not any(q.get("pausing") for q in self.queues.values()):
            for q in self.queues.values():
                self._report_vacuum(q)
            if not self._finished_reported:
                self._finished_reported = True
                now = dt_util.utcnow()
                self.hass.bus.async_fire(f"{DOMAIN}_job_finished", {
                    "job_id": self.job_id,
                    "started_at": _iso(self.started_at),
                    "finished_at": _iso(now),
                    "duration_min": round((now - self.started_at).total_seconds() / 60),
                    "vacuums": [q["vacuum"] for q in self.queues.values()],
                    "rooms_done": sorted({r for it in self.items() if it["state"] == "done" for r in it.get("rooms") or []}),
                    "blocked": [
                        {"room": r, "kind": it["kind"], "vacuum": it.get("vacuum"), "reason": it.get("reason")}
                        for it in self.items() if it["state"] == "blocked" for r in it.get("rooms") or []
                    ],
                    "warnings": list(self.warnings),
                })
            self.finish()

    # -- edits (anyvac.edit_job, docs/52 §3.4) ------------------------------------------------

    def remove_room(self, room: str, kind: str | None = None) -> list[str]:
        """Take a room out of the job. A queued item just leaves the queue; the
        item a robot is cleaning ends with a pause and the next item."""
        changed: list[str] = []
        for q in self.queues.values():
            for it in q["items"]:
                if room not in (it.get("rooms") or []) or (kind and it.get("kind") != kind):
                    continue
                if it["state"] == "queued":
                    if it.get("batch") and len(it["rooms"]) > 1:
                        pairs = [(r, s) for r, s in zip(it["rooms"], it["segments"]) if r != room]
                        it["rooms"] = [r for r, _ in pairs]
                        it["segments"] = [s for _, s in pairs]
                    else:
                        it["state"] = "removed"
                    changed.append(it["id"])
                elif it["state"] in ("sent", "active"):
                    if it.get("batch") and len(it["rooms"]) > 1:
                        raise ValueError(f"{q['vacuum']} runs {room} in a batch it already started")
                    it["state"] = "removed"
                    it["done_at"] = _iso(dt_util.utcnow())
                    q["pausing"] = {"at": _iso(dt_util.utcnow())}
                    q["phase"] = "pausing"
                    for coord in _coordinators(self.hass):
                        coord.discard_task(q["duid"])
                        coord.set_task_item(q["duid"], None)
                    self.hass.async_create_task(self._pause(q))
                    changed.append(it["id"])
        self._after_edit()
        return changed

    async def _pause(self, q: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(
                "vacuum", "pause", {"entity_id": q["vacuum"]}, blocking=True, context=self.context,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("AnyVac job %s: pause of %s failed: %s", self.job_id, q["vacuum"], err)
        self._schedule(f"pause-{q['duid']}", PAUSE_SETTLE_S)

    async def _continue_after_pause(self, q: dict[str, Any], dd: dict[str, Any], now: datetime) -> None:
        since = _parse((q.get("pausing") or {}).get("at")) or now
        if dd.get("status_state") not in ("paused", "idle", "charging", "charging_complete") and (now - since).total_seconds() < 30:
            return
        q.pop("pausing", None)
        nxt = self._next_ready(q)
        if nxt is not None:
            await self._send(q, nxt, dd)  # a paused robot takes a new task (docs/52 §2.2)
            return
        try:
            await self.hass.services.async_call(
                "vacuum", "return_to_base", {"entity_id": q["vacuum"]}, blocking=True, context=self.context,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("AnyVac job %s: return_to_base of %s failed: %s", self.job_id, q["vacuum"], err)
        q["phase"] = "returning"

    def add_items(self, items: list[dict[str, Any]], wash: dict[str, dict[str, Any]] | None = None) -> list[str]:
        """New rooms for the running job. An item for a room+kind the job
        already has (queued or running) is skipped."""
        have = {(r, it["kind"]) for it in self.items() if it["state"] not in ("removed", "blocked") for r in it.get("rooms") or []}
        added: list[str] = []
        for it in items:
            if all((r, it["kind"]) in have for r in it.get("rooms") or []):
                continue
            q = self.queues.get(it["duid"])
            if q is not None and q["batch"] and any(x["state"] in ("sent", "active") for x in q["items"]):
                raise ValueError(f"{q['vacuum']} runs a batch it already started")
            new = self._add_item(dict(it), wash=(wash or {}).get(it["duid"]))
            added.append(new["id"])
        self._after_edit()
        return added

    def retry(self, room: str, kind: str | None = None) -> list[str]:
        """A blocked room goes back into its queue where it was (the card's
        Retry, docs/52 §5 item 3) — e.g. after the user filled the tank."""
        changed: list[str] = []
        for q in self.queues.values():
            for it in q["items"]:
                if it["state"] == "blocked" and room in (it.get("rooms") or []) and (not kind or it.get("kind") == kind):
                    it["state"] = "queued"
                    it.pop("reason", None)
                    it.pop("done_at", None)
                    for k in ("sent_at", "accepted_at", "sent_from_dock", "baseline"):
                        it.pop(k, None)
                    it["attempts"] = 0
                    changed.append(it["id"])
        if changed:
            self._after_edit()
        return changed

    def move(self, room: str, kind: str, index: int) -> bool:
        """Manual order (docs/53 P2): the planner never moves it back."""
        for q in self.queues.values():
            for it in q["items"]:
                if it["state"] == "queued" and it.get("kind") == kind and room in (it.get("rooms") or []):
                    q["items"].remove(it)
                    it["manual"] = True
                    queued = [x for x in q["items"] if x["state"] == "queued"]
                    if index >= len(queued):
                        q["items"].append(it)
                    else:
                        q["items"].insert(q["items"].index(queued[max(index, 0)]), it)
                    self._after_edit()
                    return True
        return False

    def reassign(self, room: str, kind: str, item: dict[str, Any]) -> None:
        """Hand a room's pass to another robot (`item` is built for it)."""
        self.remove_room(room, kind)
        it = dict(item)
        it["manual"] = True
        self._add_item(it)
        self._after_edit()

    def _after_edit(self) -> None:
        for duid, rooms in self._scope().items():
            for coord in _coordinators(self.hass):
                coord.set_job_rooms(duid, rooms, keep_id=True)
        for q in self.queues.values():
            if any(it["state"] not in FINAL for it in q["items"]):
                q["reported"] = False
        self._save()
        self._publish()
        self.hass.async_create_task(self._locked_step())

    # -- state for the card / persistence -------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "started_at": _iso(self.started_at),
            "mode": self.mode,
            "settings": self.settings,
            "user_id": getattr(self.context, "user_id", None),
            "queues": self.queues,
            "dry_rooms_done": sorted(self.dry_rooms_done),
            "warnings": list(self.warnings),
        }

    def _signature(self) -> Any:
        return (
            tuple(
                (it["id"], it["state"], it.get("reason"), tuple(it.get("rooms") or ()), it.get("manual"))
                for it in self.items()
            ),
            tuple((d, q.get("phase"), (q.get("wash") or {}).get("mop_min_since_wash")) for d, q in self.queues.items()),
            tuple(self.warnings),
        )

    def _publish(self, force: bool = False) -> None:
        sig = self._signature()
        if not force and sig == self._published:
            return
        self._published = sig
        state = {
            "job_id": self.job_id,
            "started_at": self.started_at,
            "mode": self.mode,
            "queues": self.queues,
            "warnings": list(self.warnings),
        }
        self._publishing = True
        try:
            for coord in _coordinators(self.hass):
                setter = getattr(coord, "set_job_state", None)
                if setter is not None:
                    setter(state)
        finally:
            self._publishing = False

    def _save(self) -> None:
        for coord in _coordinators(self.hass):
            save = getattr(coord, "save_job", None)
            if save is not None:
                save(self.snapshot())

    @classmethod
    def from_snapshot(cls, hass: HomeAssistant, snap: dict[str, Any]) -> JobRunner | None:
        started = _parse(snap.get("started_at"))
        if started is None or dt_util.utcnow() - started > JOB_MAX_AGE:
            return None
        user_id = snap.get("user_id")
        runner = cls(
            hass, [], mode=snap.get("mode") or "dry", settings=snap.get("settings"),
            context=Context(user_id=user_id) if user_id else None,
            job_id=snap.get("job_id"), started_at=started,
        )
        runner.queues = {str(k): v for k, v in (snap.get("queues") or {}).items()}
        runner.dry_rooms_done = set(snap.get("dry_rooms_done") or [])
        runner.warnings = list(snap.get("warnings") or [])
        runner.started_vacuums = {q["vacuum"] for q in runner.queues.values() if q.get("first_sent_at")}
        return runner

    def _reconcile(self) -> None:
        """After a restart (docs/52 §9.5): match what each robot does now with
        the item we had sent it."""
        for duid, q in self.queues.items():
            q.pop("pausing", None)
            it = self._current(q)
            if it is None:
                continue
            dd = self._device(duid)
            state = dd.get("status_state")
            targets = set(dd.get("target_segments") or [])
            if not dd:
                it["state"], it["reason"] = "blocked", "offline"
            elif state == "error":
                it["state"], it["reason"] = "blocked", "error"
            elif dd.get("in_cleaning") and state in ACTIVE_STATES | WASH_STATES:
                if not targets or targets == set(it["segments"]):
                    it["state"] = "active"
                    for coord in _coordinators(self.hass):
                        coord.set_task_item(duid, set(it.get("rooms") or []))
                else:
                    self._warn(f"{q['vacuum']} cleans something else than the job sent; the job stopped")
                    for x in q["items"]:
                        if x["state"] not in FINAL:
                            x["state"], x["reason"] = "blocked", "robot runs another clean"
            else:
                # Not finished as far as we know: again rather than skipped.
                it["state"] = "queued"
                it["attempts"] = 0
                q["items"].remove(it)
                q["items"].insert(0, it)
        self._save()

    # -- teardown ------------------------------------------------------------------------------

    def finish(self, keep_saved: bool = False) -> None:
        """Stop listening and deregister (idempotent). ``keep_saved`` (unload)
        leaves the saved job for the next start to resume."""
        if self._closed and self not in _active_jobs(self.hass):
            return
        self._closed = True
        for unsub in self._unsub:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsub = []
        for cancel in self._timers.values():
            cancel()
        self._timers = {}
        for duid in self.queues:
            for coord in _coordinators(self.hass):
                coord.set_job_rooms(duid, None)
        for coord in _coordinators(self.hass):
            setter = getattr(coord, "set_job_state", None)
            if setter is not None:
                setter(None)
            if not keep_saved:
                save = getattr(coord, "save_job", None)
                if save is not None:
                    save(None)
        jobs = _active_jobs(self.hass)
        if self in jobs:
            jobs.remove(self)
