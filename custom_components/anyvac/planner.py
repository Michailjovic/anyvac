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

import itertools
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import battery as batterymod
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

# docs/53 §4: the order search. Up to this many rooms per robot and pass,
# every order is tried; above it, swaps and moves until nothing improves.
EXHAUSTIVE_MAX = 6
# Simulations one plan may spend (~20 µs each).
SEARCH_BUDGET = 6000


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


def travel_minutes(transit: dict[str, dict[str, float]] | None, place: str | None, rooms: list[str]) -> float:
    """Minutes from `place` to the first of `rooms` (0 without data)."""
    if not rooms or not place or place == rooms[0]:
        return 0.0
    v = ((transit or {}).get(place) or {}).get(rooms[0])
    return float(v) if isinstance(v, (int, float)) else 0.0


def battery_need(r: dict[str, Any], it: dict[str, Any], place: str | None) -> float:
    """% one queue item takes: travel there + the room, at the robot's drain
    for that kind and fan speed (docs/53 §5). 0 without a battery model."""
    bat = r.get("bat")
    if bat is None:
        return 0.0
    trav = 0.0 if it.get("here") else travel_minutes(r.get("transit"), place, it.get("rooms") or [])
    return (trav + max(float(it.get("est_min") or 0.0), 0.0)) * bat.drain_rate(it.get("kind"), it.get("fan"))


def charge_goal(rest_need: float, need: float) -> float:
    """Charge target before an item that does not fit (docs/53 P1), at least
    enough for that item above the reserve, at most 100 %."""
    return min(100.0, max(batterymod.charge_target(rest_need), batterymod.RESERVE + need + 1))


def needs_charge(battery: float | None, need: float) -> bool:
    """The item would leave less than the reserve — unless the robot is full
    already (a room bigger than a charge: the firmware recharges on its own)."""
    return battery is not None and battery < 99 and battery - need < batterymod.RESERVE


def simulate_queues(robots: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Start/finish minute of every queue item (docs/52 §3, §5a).

    ``robots`` = ``{key: {items, wash_mode, interval, batch, since_wash,
    offset, pre_wash}}``; an item is ``{id, kind, rooms, est_min}`` (for the
    item a robot is working on now, ``est_min`` is what is LEFT of it). Each
    robot runs its items in order. A wet item waits for the dry item(s) of the
    same room(s); dry items never wait. A robot takes the item under way first,
then the first item in its order whose room is ready, as the runner does.
Between two items of a room-by-room
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
        # docs/53 §3: travel between rooms. `transit` = {from: {to: minutes}}
        # with "dock"; `place` = where the robot is now. No data → no travel
        # (the model before 1.64.0).
        transit = r.get("transit") or {}
        place = r.get("place") or "dock"

        def travel(to_rooms: list[str]) -> float:
            return travel_minutes(transit, place, to_rooms)

        # docs/53 §5: battery. `battery` = % now, `bat` = `battery.Model`.
        battery = r.get("battery") if r.get("bat") is not None else None
        battery = float(battery) if isinstance(battery, (int, float)) else None
        items_all = r.get("items") or []
        pending = list(items_all)

        def gate_of(x: dict[str, Any]) -> float:
            if x.get("kind") != "wet":
                return 0.0
            return max((dry_finish.get(room, 0.0) for room in x.get("rooms") or []), default=0.0)

        first = True
        while pending:
            # The runner's choice (`_next_ready`): the item under way first, then
            # the first one in queue order whose room is ready; none ready → the
            # one whose dry pass ends first (the robot waits for it). Field
            # 2026-11-27: a strict order made a wet robot "wait" for the Corridor
            # while it was already mopping the Bathroom.
            here = [x for x in pending if x.get("here")]
            if here or batch:
                it = (here or pending)[0]
            else:
                ready = [x for x in pending if gate_of(x) <= t + 1e-9]
                it = ready[0] if ready else min(pending, key=gate_of)
            pending.remove(it)
            kind = it.get("kind")
            rooms = it.get("rooms") or []
            if battery is not None and not it.get("here"):
                need = battery_need(r, it, place)
                if needs_charge(battery, need):
                    # Home to charge first, to what the rest of the queue needs.
                    t += travel_minutes(transit, place, ["dock"]) if place != "dock" else 0.0
                    place = "dock"
                    need = battery_need(r, it, place)
                    rest = need + sum(battery_need(r, x, None) for x in pending)
                    goal = charge_goal(rest, need)
                    t += r["bat"].charge_minutes(battery, goal)
                    battery = goal
                battery -= need
            elif battery is not None:
                battery -= battery_need(r, it, place)
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
                        place = "dock"
                    else:
                        t += CHAIN_GAP_MIN
                gate = max(
                    (dry_finish.get(room, 0.0) for room in rooms),
                    default=0.0,
                )
                if gate > t + 1e-9 and not first:
                    place = "dock"  # nothing to do yet: it went home and waits
                start = max(t, gate)
            else:
                if not first and not batch:
                    t += CHAIN_GAP_MIN
                start = t
            remaining = max(float(it.get("est_min") or 0.0), 0.0)
            end = start + (0.0 if it.get("here") else travel(rooms))
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
                for room in rooms:
                    dry_finish[room] = max(dry_finish.get(room, 0.0), end)
            if rooms:
                place = rooms[-1]
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


def item_pct(dd: dict[str, Any], it: dict[str, Any]) -> int:
    """Live % of an active queue item from the room-completion tracker."""
    prog = dd.get("rooms_progress") or {}
    vals = []
    for room in it.get("rooms") or []:
        raw = (prog.get(room) or {}).get(f"{it.get('kind')}_pct")
        vals.append(int(raw) if isinstance(raw, (int, float)) else 0)
    return round(sum(vals) / len(vals)) if vals else 0


def live_robot_model(
    q: dict[str, Any],
    dd: dict[str, Any],
    transit: dict[str, dict[str, float]] | None = None,
    place: str | None = None,
    bat: Any = None,
) -> dict[str, Any]:
    """`simulate_queues` input for one robot of the running job: the items
    still to run, the active one reduced to what is left of it. Shared by the
    coordinator (`job_progress`) and the runner (when to pre-wash)."""
    items: list[dict[str, Any]] = []
    prev_kind: str | None = None
    for it in q.get("items") or []:
        st = it.get("state")
        if st in ("done", "blocked", "removed"):
            prev_kind = it.get("kind")
            continue
        est = float(it.get("est_min") or 0.0)
        if st == "active":
            est *= 1 - min(item_pct(dd, it), 100) / 100
        row = {"id": it["id"], "kind": it.get("kind"), "rooms": it.get("rooms") or [], "est_min": est,
               "fan": str(it["fan_speed"]).lower() if it.get("fan_speed") else None}
        if st in ("active", "sent"):
            row["here"] = True  # in the room or on its way: no travel left to model
        items.append(row)
    wash = q.get("wash") or {}
    since = wash.get("mop_min_since_wash") or 0.0
    return {
        "items": items,
        "wash_mode": wash.get("mode"),
        "interval": wash.get("interval_min"),
        "batch": bool(q.get("batch")),
        "since_wash": since,
        "prev_kind": prev_kind,
        # The dock washed the mop during this job and it has not mopped since:
        # the first wet room starts without the wash before it.
        "pre_wash": not (wash.get("washed_at") and not since) and dd.get("mop_dirty") is not False,
        "transit": transit or {},
        "place": place or "dock",
        "bat": bat,
        "battery": dd.get("battery"),
    }


def plan_score(robots: dict[str, dict[str, Any]]) -> tuple[float, float, float]:
    """How good a set of queues is (docs/53 §1): (end of the whole job, every
    robot back in its dock, sum of finishes) — compare as a tuple, lower wins.
    The drive home after the last room is no cleaning time, but a robot that
    ends next to its dock is free sooner."""
    times = simulate_queues(robots)
    ends = [f for _, f in times.values()]
    home = 0.0
    for r in robots.values():
        items = r.get("items") or []
        if not items:
            continue
        # The item that ends last in time (the runner may take them out of order).
        last = max(items, key=lambda x: times[x["id"]][1])
        rooms = last.get("rooms") or []
        back = ((r.get("transit") or {}).get(rooms[-1]) or {}).get("dock") if rooms else None
        home = max(home, times[last["id"]][1] + (float(back) if isinstance(back, (int, float)) else 0.0))
    return (round(max(ends, default=0.0), 3), round(home, 3), round(sum(ends), 3))


def candidate_orders(rooms: list[Any]):
    """Every order up to `EXHAUSTIVE_MAX`, else every single move of one entry."""
    if len(rooms) <= EXHAUSTIVE_MAX:
        yield from (list(p) for p in itertools.permutations(rooms))
        return
    n = len(rooms)
    yield list(rooms)
    for i in range(n):
        for j in range(n):
            if i != j:
                o = list(rooms)
                o.insert(j, o.pop(i))
                yield o


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

    def _estimate(
        self, duid: str, room: str, kind: str, repeat: int = 1, route: str | None = None
    ) -> float | None:
        """Learned minutes for `repeat` passes (estimates are per pass,
        docs/52 §5a); a wet pass prefers the estimate of its mop route."""
        ests = (self.coord.rooms_estimate.get(duid)) or {}
        rec = ests.get(room)
        if not rec and self._paired_names():
            rec = ests.get(self._own_room_name(duid, room))
        rec = rec or {}
        room_key = room if room in ests else self._own_room_name(duid, room) if self._paired_names() else room
        key = None
        for k in ((f"wet@{str(route).lower()}" if kind == "wet" and route else None), kind, "dry", "wet"):
            if k and rec.get(k):
                key = k
                break
        if key is None:
            return None
        val = float(rec[key])
        whole = getattr(self.coord, "estimate_is_whole_clean", None)
        if callable(whole) and whole(duid, room_key, key):
            return val  # learned before 1.63.0: already all passes in one
        return val * max(1, int(repeat or 1))

    def _item_estimate(self, duid: str, room: str, kind: str, settings: dict[str, Any] | None) -> float:
        ks = self._settings_for_duid(settings, kind, duid)
        repeat = max(1, int(ks.get("repeat") or 1))
        route = ks.get("mop_mode") if kind == "wet" else None
        val = self._estimate(duid, room, kind, repeat, route)
        return val if val is not None else DEFAULT_ROOM_MIN * repeat

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
        # For the order search (docs/53): who else could take a room, and
        # which rooms the user pinned (those never change robot).
        if not hasattr(self, "_search_cands"):
            self._search_cands, self._search_pinned = {}, {}
        self._search_cands[kind] = list(cands)
        self._search_pinned[kind] = set()
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
                    self._search_pinned[kind].add(room)
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
        dev = (getattr(self.coord, "data", None) or {}).get(duid)
        dd = getattr(dev, "data", None) or {}
        tm = getattr(self.coord, "transit_minutes", None)
        rp = getattr(self.coord, "robot_place", None)
        bm = getattr(self.coord, "battery_model", None)
        # A mop the dock already washed needs no wash before the first wet room.
        return {"wash_mode": mode, "interval": interval, "batch": batch,
                "pre_wash": dd.get("mop_dirty") is not False,
                "transit": tm(duid) if callable(tm) else {},
                "place": rp(duid) if callable(rp) else "dock",
                "bat": bm(duid) if callable(bm) else None,
                "battery": dd.get("battery")}

    def _estimate_timeline(
        self,
        dry_assign: dict[str, list[str]],
        wet_assign: dict[str, list[str]],
        batch: set[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finish-time estimate of a job whose queues are already in order
        (``{duid: [room, ...]}`` per pass). Thin wrapper over `simulate_queues`,
        the same model the live `job_progress` uses."""
        batch = batch or set()
        robots = self._robots_for(dry_assign, wet_assign, batch, settings)
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

    def _robots_for(
        self,
        dry_assign: dict[str, list[str]],
        wet_assign: dict[str, list[str]],
        batch: set[str],
        settings: dict[str, Any] | None,
        est: dict[tuple[str, str, str], float] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """`simulate_queues` input for queues already in order."""
        cache = est if est is not None else {}

        def e(duid: str, room: str, kind: str) -> float:
            k = (duid, room, kind)
            if k not in cache:
                cache[k] = self._item_estimate(duid, room, kind, settings)
            return cache[k]

        models = getattr(self, "_model_cache", None)

        def fan(duid: str, kind: str) -> str | None:
            f = self._settings_for_duid(settings, kind, duid).get("fan_speed")
            return str(f).lower() if f else None

        robots: dict[str, dict[str, Any]] = {}
        for kind, assign_ in (("dry", dry_assign), ("wet", wet_assign)):
            for duid, rooms in assign_.items():
                if duid not in robots:
                    key = (duid, duid in batch)
                    if models is not None and key in models:
                        base = models[key]
                    else:
                        base = self.robot_model(duid, duid in batch)
                        if models is not None:
                            models[key] = base
                    robots[duid] = {**base, "items": []}
                r = robots[duid]
                if duid in batch:
                    r["items"].append({
                        "id": f"{kind}-{duid}-batch", "kind": kind, "rooms": list(rooms),
                        "est_min": sum(e(duid, x, kind) for x in rooms),
                    })
                    continue
                for i, room in enumerate(rooms):
                    r["items"].append({
                        "id": f"{kind}-{duid}-{i}", "kind": kind, "rooms": [room],
                        "est_min": e(duid, room, kind),
                        "fan": fan(duid, kind),
                    })
        return robots

    def optimize_order(
        self,
        dry_assign: dict[str, list[str]],
        wet_assign: dict[str, list[str]],
        batch: set[str],
        settings: dict[str, Any] | None,
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """docs/53 §4: the order (and, for rooms nobody pinned, the robot)
        with the earliest end of the whole job, ties broken by the earliest
        sum of finishes. Each candidate is scored by `simulate_queues` — the
        same model as the ETA, travel and mop washes included. Batch robots
        keep the app's order (the firmware decides)."""
        est: dict[tuple[str, str, str], float] = {}
        self._model_cache = {}
        budget = [SEARCH_BUDGET]
        assigns = {"dry": {d: list(r) for d, r in dry_assign.items()},
                   "wet": {d: list(r) for d, r in wet_assign.items()}}

        def score() -> tuple[float, float, float]:
            budget[0] -= 1
            return plan_score(self._robots_for(assigns["dry"], assigns["wet"], batch, settings, est))

        best = score()
        orders = candidate_orders

        try:
            for _round in range(4):
                improved = False
                for kind in ("dry", "wet"):
                    for duid in list(assigns[kind]):
                        if duid in batch or len(assigns[kind][duid]) < 2:
                            continue
                        keep = assigns[kind][duid]
                        for o in orders(keep):
                            if budget[0] <= 0:
                                raise StopIteration
                            assigns[kind][duid] = o
                            sc = score()
                            if sc < best:
                                best, keep, improved = sc, o, True
                        assigns[kind][duid] = keep
                    # Hand a room to another robot that can do it.
                    pinned = getattr(self, "_search_pinned", {}).get(kind, set())
                    cands = getattr(self, "_search_cands", {}).get(kind, [])
                    for src in list(assigns[kind]):
                        if src in batch:
                            continue
                        for room in list(assigns[kind].get(src, [])):
                            if room in pinned:
                                continue
                            for dst in cands:
                                if dst == src or dst in batch or not self._duid_owns_room(dst, room) or not self.entity_of.get(dst):
                                    continue
                                before = {d: list(r) for d, r in assigns[kind].items()}
                                assigns[kind][src] = [x for x in assigns[kind][src] if x != room]
                                target = assigns[kind].setdefault(dst, [])
                                best_pos = None
                                for pos in range(len(target) + 1):
                                    if budget[0] <= 0:
                                        assigns[kind] = before
                                        raise StopIteration
                                    target.insert(pos, room)
                                    sc = score()
                                    if sc < best:
                                        best, best_pos = sc, pos
                                    target.pop(pos)
                                if best_pos is None:
                                    assigns[kind] = before
                                else:
                                    target.insert(best_pos, room)
                                    if not assigns[kind][src]:
                                        assigns[kind].pop(src)
                                    improved = True
                                    break
                if not improved:
                    break
        except StopIteration:
            pass
        # The queue in the order the robots will actually take it (the runner
        # picks the first READY item, so ties of the search are only display).
        times = simulate_queues(self._robots_for(assigns["dry"], assigns["wet"], batch, settings, est))
        self._model_cache = None
        for kind in ("dry", "wet"):
            for duid, rooms in assigns[kind].items():
                if duid in batch:
                    continue
                order = sorted(range(len(rooms)), key=lambda i: (times.get(f"{kind}-{duid}-{i}", (0.0, 0.0))[0], i))
                assigns[kind][duid] = [rooms[i] for i in order]
        return assigns["dry"], assigns["wet"]

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
        dry_finish = self._estimate_timeline(dry_assign, {}, batch_duids, settings)["timeline"]["dry"]
        if mode in ("wet", "both"):
            wet_assign, wet_un = self.assign(rooms, "wet", vacuums, self._pin_for_kind(pin, "wet"))
            if wet_un:
                plan["unassigned"]["wet"] = wet_un
            wet_assign = {
                d: self.order_rooms(d, rms, "wet", dry_finish, batch=d in batch_duids)
                for d, rms in wet_assign.items()
                if self.entity_of.get(d)
            }

        dry_assign, wet_assign = self.optimize_order(dry_assign, wet_assign, batch_duids, settings)
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

        timeline = self._estimate_timeline(dry_assign, wet_assign, batch_duids, settings)
        if not plan["unassigned"]:
            plan.pop("unassigned")
        if batch_duids:
            plan["batch"] = sorted(self.entity_of.get(d) or d for d in batch_duids)
        plan.update(timeline)
        return items, plan

    def settings_fields(self, duid: str, kind: str, settings: dict[str, Any] | None) -> dict[str, Any]:
        """The settings part of a queue item: what to send, what the robot
        must then report (`expect`), and which mop selects to watch for a
        change the user makes on the robot (docs/52 §3.3)."""
        kind_settings = self._settings_for_duid(settings, kind, duid)
        selects, fan = self._settings_calls(duid, kind, kind_settings)
        mop_mode = kind_settings.get("mop_mode") if kind == "wet" else None
        return {
            "watch": sorted(set(selects_for_duid(self.hass, duid).values())),
            "selects": selects,
            "fan_speed": fan,
            "repeat": max(1, int(kind_settings.get("repeat") or 1)),
            "expect": {
                "fan_speed": fan,
                "mop_mode": mop_mode,
                "water_off": kind == "dry" and any(
                    s.get("option") == "off" for s in selects
                ),
            },
        }

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
        return {
            "id": item_id,
            "vacuum": self.entity_of.get(duid),
            "duid": duid,
            "kind": kind,
            "batch": batch,
            "room": rooms[0] if len(rooms) == 1 else None,
            "rooms": list(rooms),
            "segments": [self._segment_for(duid, r) for r in rooms],
            **self.settings_fields(duid, kind, settings),
            "est_min": round(sum(self._item_estimate(duid, r, kind, settings) for r in rooms), 1),
        }
