"""Server-side clean planning (kontrakt v2, docs/14 §3.7).

The card sends an INTENT (rooms + mode + optional restrictions/settings) to
``anyvac.clean``; everything the card used to compute client-side — capability
detection, LPT assignment, segment resolution, dry→wet gating, per-room pinning —
is built here from the coordinator's own data. The output is a list of queue
items, one per room and pass (docs/52): the job runner sends them to the robots
one at a time, so the order, the settings and every edit stay under AnyVac's
control instead of the firmware's.

Room keys are Roborock-app room names (canon rule 5); internal matching against
the robot maps goes through ``segment_id``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import ROBOROCK_DOMAIN

_LOGGER = logging.getLogger(__name__)

# Weight used for LPT balancing when no learned estimate exists yet. Minimum load
# per room is 1 so a fresh install still round-robins instead of collapsing onto
# the first capable robot (same rule as the card's _assignByCap).
DEFAULT_ROOM_MIN = 15.0

# Wall-clock cost of one mop-wash episode during a wet pass (docs/23 §6): the
# dock positions itself (~15s) then washes (~2min), field-observed in docs/17
# ("wash je vždy viditelný"). Room-time estimates (`rooms_estimate`) deliberately
# EXCLUDE this — coordinator attribution freezes during a wash (docs/16/docs/14
# rule 4) — so without adding it back here the ETA silently omits every wash
# break a wet session takes.
WASH_DURATION_MIN = 2.25

# docs/52 §8 (HW test 2026-11-25): a room-by-room robot gets its next room on
# the way home ~20 s after finishing the previous one; a wet robot that docks
# to wash at the end of a task loses ~6.5 min (drive home + ~5.5 min wash); a
# job that starts from the dock with a used mop washes first (~3 min).
CHAIN_GAP_MIN = 0.35
END_WASH_MIN = 6.5
PRE_WASH_MIN = 3.0


def vacuum_entity_for_duid(hass: HomeAssistant, duid: str) -> str | None:
    """vacuum.* entity of the Roborock device with this duid (device registry)."""
    device = dr.async_get(hass).async_get_device(identifiers={(ROBOROCK_DOMAIN, duid)})
    if device is None:
        return None
    for ent in er.async_entries_for_device(er.async_get(hass), device.id):
        if ent.domain == "vacuum" and not ent.disabled_by:
            return ent.entity_id
    return None


def duid_for_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Roborock duid for any entity belonging to the vacuum's device."""
    ent = er.async_get(hass).async_get(entity_id)
    if ent is None or not ent.device_id:
        return None
    device = dr.async_get(hass).async_get(ent.device_id)
    if device is None:
        return None
    for domain, ident in device.identifiers:
        if domain == ROBOROCK_DOMAIN:
            return ident
    return None


def selects_for_duid(hass: HomeAssistant, duid: str) -> dict[str, str]:
    """Mop-related select entities of the device: {mop_mode, mop_intensity}."""
    out: dict[str, str] = {}
    device = dr.async_get(hass).async_get_device(identifiers={(ROBOROCK_DOMAIN, duid)})
    if device is None:
        return out
    for ent in er.async_entries_for_device(er.async_get(hass), device.id):
        if ent.domain != "select" or ent.disabled_by:
            continue
        key = ent.translation_key or ""
        if key in ("mop_mode", "mop_intensity"):
            out.setdefault(key, ent.entity_id)
            continue
        # Fallback for entity-id based matching when no translation key is set.
        for k in ("mop_mode", "mop_intensity"):
            if k in ent.entity_id:
                out.setdefault(k, ent.entity_id)
    return out


def simulate_queues(robots: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Start/finish minute of every queue item (docs/52 §3, §5a).

    ``robots`` = ``{key: {items, wash_mode, interval, batch, since_wash,
    offset, pre_wash}}``; an item is ``{id, kind, rooms, est_min}`` (for the
    item a robot is working on now, ``est_min`` is what is LEFT of it). Each
    robot runs its items in order. A wet item waits for the dry item(s) of the
    same room(s); dry items never wait. Between two items of a room-by-room
    robot: ``CHAIN_GAP_MIN`` when the next one is sent on the way home, or
    ``END_WASH_MIN`` when a wet robot docks to wash first (after every room in
    the app's by-room mode, once the mopping since the last wash reaches the
    interval in by-time mode). Inside one task the firmware washes on its own
    interval (``WASH_DURATION_MIN`` each). A batch robot is one task.

    The single timing model of AnyVac: the planner uses it for the plan, the
    coordinator for the live ``job_progress`` (docs/14 rule 2)."""
    times: dict[str, tuple[float, float]] = {}
    dry_finish: dict[str, float] = {}

    def run_robot(r: dict[str, Any]) -> None:
        t = float(r.get("offset") or 0.0)
        mode, interval, batch = r.get("wash_mode"), r.get("interval"), bool(r.get("batch"))
        since_wash = float(r.get("since_wash") or 0.0)
        prev_kind: str | None = r.get("prev_kind")
        first = True
        for it in r.get("items") or []:
            kind = it.get("kind")
            if kind == "wet":
                if first and mode and r.get("pre_wash", True) and prev_kind is None:
                    t += PRE_WASH_MIN
                elif not first and not batch:
                    wash_now = prev_kind == "wet" and (
                        mode in ("room", "unknown")
                        or (mode == "time" and interval and since_wash >= interval)
                    )
                    if wash_now:
                        t += END_WASH_MIN
                        since_wash = 0.0
                    else:
                        t += CHAIN_GAP_MIN
                gate = max(
                    (dry_finish.get(room, 0.0) for room in it.get("rooms") or []),
                    default=0.0,
                )
                start = max(t, gate)
            else:
                if not first and not batch:
                    t += CHAIN_GAP_MIN
                start = t
            remaining = max(float(it.get("est_min") or 0.0), 0.0)
            end = start
            if kind == "wet" and interval and interval > 0:
                # The firmware's own counter only runs inside one task.
                in_task = since_wash if batch else 0.0
                while in_task + remaining >= interval:
                    chunk = interval - in_task
                    end += chunk + WASH_DURATION_MIN
                    remaining -= chunk
                    in_task = 0.0
                    since_wash = 0.0
                since_wash += remaining
            end += remaining
            t = end
            times[it["id"]] = (start, end)
            if kind == "dry":
                for room in it.get("rooms") or []:
                    dry_finish[room] = max(dry_finish.get(room, 0.0), end)
            prev_kind = kind
            first = False

    # Gates point from wet items to dry items, possibly of another robot, and a
    # both-capable robot may run its wet items before somebody's dry ones —
    # iterate to a fixed point (a few rounds always settle: gates are acyclic).
    for _ in range(6):
        before = dict(times)
        for r in robots.values():
            run_robot(r)
        if times == before:
            break
    return times


class CleanPlanner:
    """Builds an executable task plan from a clean intent."""

    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:
        self.hass = hass
        self.coord = coordinator
        devices = coordinator.data or {}
        # Per-duid room-name -> segment_id map (what each robot can actually clean).
        self.segments: dict[str, dict[str, int]] = {}
        # docs/40 §4.3 (Fáze 2.4): cross-robot pairing of the SAME physical room
        # by the home frame's shared `home_room_id` — robots may name one room
        # differently in their own apps (docs/30 §4b).
        #   home_room_owners:     {home_room_id: {duid: that robot's OWN segment_id}}
        #   home_room_id_by_name: {room name as some robot calls it: home_room_id}
        # Both stay empty until a home frame has paired rooms, so a fleet
        # without one behaves exactly as before (exact-name matching only).
        self.home_room_owners: dict[str, dict[str, int]] = {}
        self.home_room_id_by_name: dict[str, str] = {}
        for duid, dev in devices.items():
            segs: dict[str, int] = {}
            for r in dev.data.get("rooms", []):
                if r.get("name") is not None and r.get("segment_id") is not None:
                    segs[str(r["name"])] = int(r["segment_id"])
                    hrid = r.get("home_room_id")
                    if hrid:
                        self.home_room_owners.setdefault(str(hrid), {})[duid] = int(r["segment_id"])
                        # First robot to publish a name wins that name's entry.
                        self.home_room_id_by_name.setdefault(str(r["name"]), str(hrid))
            self.segments[duid] = segs
        self.entity_of: dict[str, str | None] = {
            duid: vacuum_entity_for_duid(hass, duid) for duid in devices
        }
        self.duid_of_entity: dict[str, str] = {
            ent: duid for duid, ent in self.entity_of.items() if ent
        }
        self.devices = devices

    # -- capabilities & estimates ------------------------------------------------

    def _capable(self, duid: str, kind: str) -> bool:
        """Intrinsic capability: everyone vacuums; wet needs an electronic water box."""
        if kind == "dry":
            return True
        sig = (self.devices[duid].data.get("mop_signal")) or {}
        return sig.get("water_box_mode") is not None or bool(sig.get("water_mode_name"))

    # -- room ownership (exact name first, then home_room_id pairing) ----------------

    def _paired_names(self) -> dict[str, str]:
        # getattr: several unit tests build the planner via object.__new__
        # without the home-frame maps; absent == no pairing.
        return getattr(self, "home_room_id_by_name", None) or {}

    def _pair_owners(self) -> dict[str, dict[str, int]]:
        return getattr(self, "home_room_owners", None) or {}

    def _duid_owns_room(self, duid: str, room: str) -> bool:
        """Can this robot clean `room`? Its own name for the room, or — when a
        home frame paired rooms — the same physical room under another robot's
        name (docs/40 §4.3)."""
        if room in self.segments.get(duid, {}):
            return True
        hrid = self._paired_names().get(room)
        return hrid is not None and duid in self._pair_owners().get(hrid, {})

    def _segment_for(self, duid: str, room: str) -> int:
        """This robot's OWN segment id for `room` (never another robot's).
        Raises KeyError when the robot does not own the room at all."""
        own = self.segments.get(duid, {})
        if room in own:
            return own[room]
        hrid = self._paired_names().get(room)
        if hrid is not None and duid in self._pair_owners().get(hrid, {}):
            return self._pair_owners()[hrid][duid]
        raise KeyError(room)

    def _own_room_name(self, duid: str, room: str) -> str:
        """The name THIS robot uses for `room` (learned estimates are keyed by
        it); `room` itself when it is already that name or unknown."""
        own = self.segments.get(duid, {})
        if room in own:
            return room
        try:
            seg = self._segment_for(duid, room)
        except KeyError:
            return room
        return next((n for n, s in own.items() if s == seg), room)

    def _estimate(self, duid: str, room: str, kind: str) -> float | None:
        ests = (self.coord.rooms_estimate.get(duid)) or {}
        rec = ests.get(room)
        if not rec and self._paired_names():
            rec = ests.get(self._own_room_name(duid, room))
        rec = rec or {}
        val = rec.get(kind) or rec.get("dry") or rec.get("wet")
        return float(val) if val else None

    def _resolve_duid(self, ref: str) -> str | None:
        """Accept either a duid or any entity id of the vacuum."""
        if ref in self.devices:
            return ref
        return self.duid_of_entity.get(ref) or duid_for_entity(self.hass, ref)

    def _allowed(self, kind: str, vacuums: Any) -> set[str]:
        """Apply the optional vacuum restriction (flat list or {dry: [], wet: []})."""
        refs: list[str] | None = None
        if isinstance(vacuums, dict):
            refs = vacuums.get(kind)
        elif isinstance(vacuums, list) and vacuums:
            refs = vacuums
        if not refs:
            return set(self.devices)
        out: set[str] = set()
        for ref in refs:
            duid = self._resolve_duid(str(ref))
            if duid:
                out.add(duid)
        return out

    # -- assignment ----------------------------------------------------------------

    @staticmethod
    def _pin_for_kind(
        pin: dict[str, dict[str, str]] | None, kind: str
    ) -> dict[str, str]:
        """Flatten the per-room {"dry"/"wet": vacuum} pin map (docs/18 §7e,
        per-kind since 2026-07-25) down to a single kind's {room: vacuum},
        the shape ``assign()`` expects. Dry and wet are resolved independently
        so pinning one pass never overrides/clobbers the other."""
        if not pin:
            return {}
        out: dict[str, str] = {}
        for room, kinds in pin.items():
            v = kinds.get(kind) if isinstance(kinds, dict) else None
            if v:
                out[room] = v
        return out

    def assign(
        self,
        rooms: list[str],
        kind: str,
        vacuums: Any = None,
        pin: dict[str, str] | None = None,
    ) -> tuple[dict[str, list[str]], list[str]]:
        """LPT greedy: biggest room first → least-loaded capable owner (mirrors the
        card's proven _assignByCap). ``pin`` {room: vacuum} overrides the choice when
        the pinned robot knows the room and is capable of the kind; otherwise that
        room falls back to normal assignment. Returns (assignment, unassigned)."""
        allowed = self._allowed(kind, vacuums)
        cands = [d for d in self.devices if d in allowed and self._capable(d, kind)]
        out: dict[str, list[str]] = {}
        load: dict[str, float] = {d: 0.0 for d in cands}
        unassigned: list[str] = []

        def est_max(room: str) -> float:
            vals = [v for d in cands if (v := self._estimate(d, room, kind))]
            return max(vals) if vals else DEFAULT_ROOM_MIN

        pins = pin or {}
        for room in sorted(rooms, key=est_max, reverse=True):
            owners = [d for d in cands if self._duid_owns_room(d, room)]
            pref = pins.get(room)
            if pref:
                pduid = self._resolve_duid(str(pref))
                if pduid and pduid in cands and self._duid_owns_room(pduid, room):
                    owners = [pduid]
                else:
                    _LOGGER.warning(
                        "AnyVac clean: pin %s -> %s not applicable for %s pass; "
                        "falling back to automatic assignment",
                        room,
                        pref,
                        kind,
                    )
            if not owners:
                unassigned.append(room)
                continue
            best = min(owners, key=lambda d: load.get(d, 0.0))
            out.setdefault(best, []).append(room)
            load[best] = load.get(best, 0.0) + max(
                self._estimate(best, room, kind) or DEFAULT_ROOM_MIN, 1.0
            )
        return out, unassigned

    # -- queue order + timing (docs/52 §3, §5a) -----------------------------------

    def wash_mode(self, duid: str) -> tuple[str | None, float | None]:
        """How the robot's dock decides to wash the mop (docs/52 §3.2).

        ``("room", interval)`` when the app is set to wash after every room
        (``smart_wash``; the interval is a hidden leftover, possibly still a cap
        inside a long room — docs/52 V8), ``("time", interval)`` for washing by
        time, ``("unknown", None)`` for a washing dock whose setting is not
        readable, ``(None, None)`` for a robot whose dock cannot wash at all."""
        dev = self.devices.get(duid)
        dock = (dev.data.get("dock_status") or {}) if dev is not None else {}
        interval = dock.get("wash_interval_min")
        if dock.get("smart_wash_enabled"):
            return "room", interval
        if interval:
            return "time", interval
        if (dock.get("features") or {}).get("is_washable"):
            # A washing dock we cannot read the setting of: let it dock and
            # wash after every wet room — the safe choice (docs/52 §3.2).
            return "unknown", None
        return None, None

    def wash_info(self, duid: str) -> dict[str, Any]:
        """The job runner's wash state of one robot (docs/52 §9.4 `wash`)."""
        mode, interval = self.wash_mode(duid)
        return {"mode": mode, "interval_min": interval, "mop_min_since_wash": 0.0}

    def _app_sequence(self, duid: str) -> dict[str, int]:
        """{room name: position} of the Roborock app's room order, read from the
        robot (`get_clean_sequence`). Only a batch robot's firmware follows it."""
        seqs = getattr(self.coord, "app_sequence", None) or {}
        return dict(seqs.get(duid) or {})

    def order_rooms(
        self,
        duid: str,
        rooms: list[str],
        kind: str,
        dry_finish: dict[str, float] | None = None,
        batch: bool = False,
    ) -> list[str]:
        """Queue order of one robot's rooms for one pass.

        Batch robots run in the app's order (the firmware decides, docs/19).
        Room-by-room robots: dry shortest first, so the slow mopping can start
        as early as possible; wet in the order their dry pass finishes. The
        full planner (docs/53) replaces this ordering."""
        if batch:
            seq = self._app_sequence(duid)
            return sorted(rooms, key=lambda r: (seq.get(r, 10_000), r))

        def est(r: str) -> float:
            return self._estimate(duid, r, kind) or DEFAULT_ROOM_MIN

        if kind == "wet" and dry_finish:
            return sorted(rooms, key=lambda r: (dry_finish.get(r, 0.0), est(r), r))
        return sorted(rooms, key=lambda r: (est(r), r))

    def robot_model(self, duid: str, batch: bool = False) -> dict[str, Any]:
        """What `simulate_queues` needs to know about one robot."""
        mode, interval = self.wash_mode(duid)
        return {"wash_mode": mode, "interval": interval, "batch": batch}

    def _estimate_timeline(
        self,
        dry_assign: dict[str, list[str]],
        wet_assign: dict[str, list[str]],
        batch: set[str] | None = None,
    ) -> dict[str, Any]:
        """Finish-time estimate of a job whose queues are already in order
        (``{duid: [room, ...]}`` per pass). Thin wrapper over `simulate_queues`,
        the same model the live `job_progress` uses."""
        batch = batch or set()
        robots: dict[str, dict[str, Any]] = {}
        for kind, assign_ in (("dry", dry_assign), ("wet", wet_assign)):
            for duid, rooms in assign_.items():
                r = robots.setdefault(duid, {**self.robot_model(duid, duid in batch), "items": []})
                if duid in batch:
                    r["items"].append({
                        "id": f"{kind}-{duid}-batch", "kind": kind, "rooms": list(rooms),
                        "est_min": sum(self._estimate(duid, x, kind) or DEFAULT_ROOM_MIN for x in rooms),
                    })
                    continue
                for i, room in enumerate(rooms):
                    r["items"].append({
                        "id": f"{kind}-{duid}-{i}", "kind": kind, "rooms": [room],
                        "est_min": self._estimate(duid, room, kind) or DEFAULT_ROOM_MIN,
                    })
        times = simulate_queues(robots)
        room_finish: dict[str, dict[str, float]] = {"dry": {}, "wet": {}}
        queues: dict[str, list[dict[str, Any]]] = {}
        for duid, r in robots.items():
            rows = queues.setdefault(self.entity_of.get(duid) or duid, [])
            for it in r["items"]:
                start, finish = times[it["id"]]
                for room in it["rooms"]:
                    room_finish[it["kind"]][room] = finish
                rows.append({
                    "room": it["rooms"][0] if len(it["rooms"]) == 1 else None,
                    "rooms": it["rooms"], "kind": it["kind"],
                    "start_min": round(start, 1), "finish_min": round(finish, 1),
                })
        eta = max((f for _, f in times.values()), default=0.0)
        return {
            "eta_min": round(eta),
            "timeline": room_finish,
            "queues": queues,
            # Kept for older cards: the room order no longer depends on the app.
            "unsequenced": [],
        }

    def _settings_for_duid(
        self, settings: dict[str, Any] | None, kind: str, duid: str
    ) -> dict[str, Any]:
        """Resolve one vacuum's settings for a pass from the per-vacuum
        ``{kind: {vacuum_ref: {...}}}`` map (2026-07-26). Settings used to be a
        single object shared by every vacuum doing that pass — whichever vacuum's
        preset was picked client-side silently won for ALL vacuums of that kind
        (e.g. S6's fan_speed overriding S7's). Each vacuum now gets its own entry,
        resolved the same way pins/vacuum-restrictions are (duid or any entity id
        of the device)."""
        kind_settings = (settings or {}).get(kind)
        if not isinstance(kind_settings, dict):
            return {}
        for ref, s in kind_settings.items():
            if isinstance(s, dict) and self._resolve_duid(str(ref)) == duid:
                return s
        return {}

    def _settings_calls(
        self, duid: str, kind: str, settings: dict[str, Any]
    ) -> tuple[list[dict[str, str]], str | None]:
        """Pre-clean selects + fan speed for one vacuum and kind. A dry pass forces
        the mop intensity off (when the robot has that select), so a wet-capable
        robot genuinely cleans dry — same rule the card applied."""
        sels = selects_for_duid(self.hass, duid)
        selects: list[dict[str, str]] = []
        if kind == "wet":
            if sels.get("mop_mode") and settings.get("mop_mode"):
                selects.append(
                    {"entity_id": sels["mop_mode"], "option": str(settings["mop_mode"])}
                )
            if sels.get("mop_intensity") and settings.get("mop_intensity"):
                selects.append(
                    {
                        "entity_id": sels["mop_intensity"],
                        "option": str(settings["mop_intensity"]),
                    }
                )
        elif sels.get("mop_intensity"):
            selects.append({"entity_id": sels["mop_intensity"], "option": "off"})
        return selects, settings.get("fan_speed")

    def build_tasks(
        self,
        rooms: list[str],
        mode: str,
        vacuums: Any = None,
        pin: dict[str, dict[str, str]] | None = None,
        settings: dict[str, dict[str, dict[str, Any]]] | None = None,
        batch: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build the job's queue items + a plan summary (docs/52 §9.1).

        Room-by-room (default): one item per room and pass, in queue order per
        robot. A wet item waits for the dry item of the same room — gating is
        by room, so it stays right through every later edit of the queues.
        Batch robots (``batch``, docs/52 R7): one item per pass with all the
        robot's rooms in the app's order; its wet item waits for all of them.

        ``settings`` is ``{"dry"/"wet": {vacuum_ref: {fan_speed, mop_mode,
        mop_intensity, repeat}}}``, per vacuum (2026-07-26).
        """
        batch_duids = {d for ref in (batch or []) if (d := self._resolve_duid(str(ref)))}
        plan: dict[str, Any] = {"dry": {}, "wet": {}, "unassigned": {}}

        dry_assign: dict[str, list[str]] = {}
        wet_assign: dict[str, list[str]] = {}
        if mode in ("dry", "both"):
            dry_assign, dry_un = self.assign(rooms, "dry", vacuums, self._pin_for_kind(pin, "dry"))
            if dry_un:
                plan["unassigned"]["dry"] = dry_un
            dry_assign = {
                d: self.order_rooms(d, rms, "dry", batch=d in batch_duids)
                for d, rms in dry_assign.items()
                if self.entity_of.get(d)
            }
        dry_finish = self._estimate_timeline(dry_assign, {}, batch_duids)["timeline"]["dry"]
        if mode in ("wet", "both"):
            wet_assign, wet_un = self.assign(rooms, "wet", vacuums, self._pin_for_kind(pin, "wet"))
            if wet_un:
                plan["unassigned"]["wet"] = wet_un
            wet_assign = {
                d: self.order_rooms(d, rms, "wet", dry_finish, batch=d in batch_duids)
                for d, rms in wet_assign.items()
                if self.entity_of.get(d)
            }

        items: list[dict[str, Any]] = []
        for kind, assign_ in (("dry", dry_assign), ("wet", wet_assign)):
            for duid, rms in assign_.items():
                plan[kind][self.entity_of[duid]] = list(rms)
                groups = [list(rms)] if duid in batch_duids else [[r] for r in rms]
                for i, group in enumerate(groups):
                    items.append(
                        self.make_item(
                            duid, kind, group, settings,
                            item_id=f"{kind}-{duid}-{'batch' if duid in batch_duids else i}",
                            batch=duid in batch_duids,
                        )
                    )

        timeline = self._estimate_timeline(dry_assign, wet_assign, batch_duids)
        if not plan["unassigned"]:
            plan.pop("unassigned")
        if batch_duids:
            plan["batch"] = sorted(self.entity_of.get(d) or d for d in batch_duids)
        plan.update(timeline)
        return items, plan

    def make_item(
        self,
        duid: str,
        kind: str,
        rooms: list[str],
        settings: dict[str, Any] | None,
        item_id: str,
        batch: bool = False,
    ) -> dict[str, Any]:
        """One queue item: everything the job runner needs to send it, check
        it was taken and re-apply the settings after the firmware reset them
        (docs/52 §3.3)."""
        kind_settings = self._settings_for_duid(settings, kind, duid)
        selects, fan = self._settings_calls(duid, kind, kind_settings)
        mop_mode = kind_settings.get("mop_mode") if kind == "wet" else None
        return {
            "id": item_id,
            "vacuum": self.entity_of.get(duid),
            "duid": duid,
            "kind": kind,
            "batch": batch,
            "room": rooms[0] if len(rooms) == 1 else None,
            "rooms": list(rooms),
            "segments": [self._segment_for(duid, r) for r in rooms],
            "selects": selects,
            "fan_speed": fan,
            "repeat": max(1, int(kind_settings.get("repeat") or 1)),
            # What the robot must report once it runs (`mop_signal`).
            "expect": {
                "fan_speed": fan,
                "mop_mode": mop_mode,
                "water_off": kind == "dry" and any(
                    s.get("option") == "off" for s in selects
                ),
            },
            "est_min": round(sum(
                self._estimate(duid, r, kind) or DEFAULT_ROOM_MIN for r in rooms
            ), 1),
        }
