"""Server-side clean planning (kontrakt v2, docs/14 §3.7).

The card sends an INTENT (rooms + mode + optional restrictions/settings) to
``anyvac.clean``; everything the card used to compute client-side — capability
detection, LPT assignment, segment resolution, dry→wet gating, per-room pinning —
is built here from the coordinator's own data. The output is the same task list
format the proven ``run_job`` executor consumes, so execution semantics are
identical to the field-tested card plans.

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


class CleanPlanner:
    """Builds an executable task plan from a clean intent."""

    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:
        self.hass = hass
        self.coord = coordinator
        devices = coordinator.data or {}
        # Per-duid room-name -> segment_id map (what each robot can actually clean).
        self.segments: dict[str, dict[str, int]] = {}
        for duid, dev in devices.items():
            segs: dict[str, int] = {}
            for r in dev.data.get("rooms", []):
                if r.get("name") is not None and r.get("segment_id") is not None:
                    segs[str(r["name"])] = int(r["segment_id"])
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

    def _estimate(self, duid: str, room: str, kind: str) -> float | None:
        rec = ((self.coord.rooms_estimate.get(duid)) or {}).get(room) or {}
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
            owners = [d for d in cands if room in self.segments.get(d, {})]
            pref = pins.get(room)
            if pref:
                pduid = self._resolve_duid(str(pref))
                if pduid and pduid in cands and room in self.segments.get(pduid, {}):
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

    # -- timing estimate (docs/19) ---------------------------------------------------

    def _estimate_timeline(
        self,
        dry_assign: dict[str, list[str]],
        wet_assign: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Sequence-aware completion estimate.

        The Roborock app's configured room order is dominant regardless of what
        order HA sends segment ids in — the firmware always visits rooms in that
        order (confirmed in the field across thousands of cleans, docs/19). We
        don't control it, we just need to KNOW it: `coordinator.room_sequence` is
        the user-maintained {room: 1-based position}. Rooms missing a position
        sort after all sequenced ones (stable, so ties keep assignment order) —
        the plan's ``unsequenced`` list flags them so the card can warn.

        Replaces the old client-side estimate, which summed
        ``max(any vacuum's room estimate)`` per room with no notion of sequence,
        parallelism or dry→wet gating — for a room needing both passes it took
        ``max(dry, wet)`` instead of ``dry + wet``, i.e. assumed a simultaneous
        start that never happens.

        Gating mirrors the REAL runtime gate (``anyvac_room_done`` is per room,
        not "wait for the whole dry batch"): a wet room's start is the specific
        dry-assigning robot's cumulative time up to (and including) that room,
        not that robot's total dry session.
        """
        seq = self.coord.room_sequence or {}
        unsequenced: set[str] = set()

        def ordered(rooms: list[str]) -> list[str]:
            def key(r: str) -> float:
                if r not in seq:
                    unsequenced.add(r)
                return seq.get(r, float("inf"))
            return sorted(rooms, key=key)

        dry_robot_finish: dict[str, float] = {}
        room_dry_finish: dict[str, float] = {}
        for duid, rooms in dry_assign.items():
            t = 0.0
            for room in ordered(rooms):
                t += self._estimate(duid, room, "dry") or DEFAULT_ROOM_MIN
                room_dry_finish[room] = t
            dry_robot_finish[duid] = t

        wet_robot_finish: dict[str, float] = {}
        room_wet_finish: dict[str, float] = {}
        for duid, rooms in wet_assign.items():
            own_dry_finish = dry_robot_finish.get(duid, 0.0) if duid in dry_assign else 0.0
            # Mop wash cadence (docs/23 §6) — None on a dock that can't wash the mop
            # at all (empty-only dock, no dock, coordinator.py `wash_interval_min`).
            dev = self.devices.get(duid)
            wash_interval = (
                (dev.data.get("dock_status") or {}).get("wash_interval_min")
                if dev is not None
                else None
            )
            t = 0.0
            active_since_wash = 0.0
            for room in ordered(rooms):
                gate = room_dry_finish.get(room, 0.0)
                start = max(t, gate, own_dry_finish)
                remaining = self._estimate(duid, room, "wet") or DEFAULT_ROOM_MIN
                # Spend this room's active cleaning time, inserting a wash break
                # (and resetting the cadence counter) every time it crosses the
                # configured interval — may fire more than once within one long
                # room, or not at all within a short one.
                while (
                    wash_interval
                    and wash_interval > 0
                    and active_since_wash + remaining >= wash_interval
                ):
                    chunk = wash_interval - active_since_wash
                    start += chunk + WASH_DURATION_MIN
                    remaining -= chunk
                    active_since_wash = 0.0
                start += remaining
                active_since_wash += remaining
                t = start
                room_wet_finish[room] = t
            wet_robot_finish[duid] = t

        eta = max(
            [f for d, f in dry_robot_finish.items() if d not in wet_assign] +
            list(wet_robot_finish.values()),
            default=0.0,
        )
        return {
            "eta_min": round(eta),
            "timeline": {"dry": room_dry_finish, "wet": room_wet_finish},
            "unsequenced": sorted(unsequenced),
        }

    # -- task building ---------------------------------------------------------------

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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build the run_job task list + a human-readable plan summary.

        Dry tasks start immediately. Wet tasks gate on the dry robot's per-room
        ``anyvac_room_done`` (matched by duid + room name) and — when the same robot
        also runs a dry pass — on its own session finishing. Repeat is passed to the
        firmware in ``app_segment_clean`` (no dock-restart hacks, docs/14 §3.8).

        ``settings`` is ``{"dry"/"wet": {vacuum_ref: {fan_speed, mop_mode,
        mop_intensity, repeat}}}`` — per-vacuum since 2026-07-26 (see
        ``_settings_for_duid``), because a fleet with several same-kind vacuums
        (e.g. two dry-only robots) needs each to keep its own preset rather than
        one shared per pass.

        docs/23 (2026-07-18): a wet-capable robot with 2+ assigned rooms in a
        ``both`` job gets a **pool task** instead of one static all-or-nothing
        task, so ``_JobRunner`` can dispatch a first batch as soon as SOME of
        its rooms are ready rather than waiting for all of them — a single
        wet-capable robot covering both a quick room and a slow one no longer
        sits idle until the slow one catches up. A robot with just one room
        (or a standalone ``wet`` job with no dry gating at all) has nothing to
        stagger and keeps the plain static task.
        """
        settings = settings or {}
        tasks: list[dict[str, Any]] = []
        plan: dict[str, Any] = {"dry": {}, "wet": {}, "unassigned": {}}

        dry_assign: dict[str, list[str]] = {}
        wet_assign: dict[str, list[str]] = {}
        if mode in ("dry", "both"):
            dry_assign, dry_un = self.assign(
                rooms, "dry", vacuums, self._pin_for_kind(pin, "dry")
            )
            if dry_un:
                plan["unassigned"]["dry"] = dry_un
        room_dry_duid: dict[str, str] = {}
        for i, (duid, rms) in enumerate(dry_assign.items()):
            entity = self.entity_of.get(duid)
            if not entity:
                _LOGGER.warning("AnyVac clean: no vacuum entity for duid %s; skipping", duid)
                continue
            kind_settings = self._settings_for_duid(settings, "dry", duid)
            selects, fan = self._settings_calls(duid, "dry", kind_settings)
            segs = [self.segments[duid][r] for r in rms]
            repeat = max(1, int(kind_settings.get("repeat") or 1))
            tasks.append(
                {
                    "id": f"dry{i}",
                    "vacuum": entity,
                    "selects": selects,
                    "fan_speed": fan,
                    "service": "vacuum.send_command",
                    "service_data": {
                        "entity_id": entity,
                        "command": "app_segment_clean",
                        "params": [{"segments": segs, "repeat": repeat}],
                    },
                }
            )
            plan["dry"][entity] = list(rms)
            for r in rms:
                room_dry_duid[r] = duid

        timeline: dict[str, Any] | None = None
        if mode in ("wet", "both"):
            wet_assign, wet_un = self.assign(
                rooms, "wet", vacuums, self._pin_for_kind(pin, "wet")
            )
            if wet_un:
                plan["unassigned"]["wet"] = wet_un
            # docs/23: the pool tasks' per-room `eta_min` hint needs the
            # dry-finish timeline, so compute it now (right after wet_assign is
            # known) instead of at the very end — reused below for `plan.update`.
            timeline = self._estimate_timeline(dry_assign, wet_assign)
            room_dry_finish = timeline["timeline"]["dry"]
            for j, (duid, rms) in enumerate(wet_assign.items()):
                entity = self.entity_of.get(duid)
                if not entity:
                    _LOGGER.warning(
                        "AnyVac clean: no vacuum entity for duid %s; skipping", duid
                    )
                    continue
                kind_settings = self._settings_for_duid(settings, "wet", duid)
                selects, fan = self._settings_calls(duid, "wet", kind_settings)
                repeat = max(1, int(kind_settings.get("repeat") or 1))
                own_gate = {"duid": duid} if duid in dry_assign else None

                if mode == "both" and len(rms) >= 2:
                    # Pool task (docs/23): per-room gate + a timing hint for the
                    # wait-vs-go decision, no single all-or-nothing `after` list.
                    pool = {
                        r: {
                            "gate": {"duid": room_dry_duid[r], "room": r},
                            "eta_min": room_dry_finish.get(r, 0.0),
                            "segment": self.segments[duid][r],
                        }
                        for r in rms
                        if r in room_dry_duid
                    }
                    tasks.append(
                        {
                            "id": f"wet{j}",
                            "vacuum": entity,
                            "duid": duid,
                            "pool": pool,
                            "selects": selects,
                            "fan_speed": fan,
                            "repeat": repeat,
                            "own_gate": own_gate,
                        }
                    )
                else:
                    segs = [self.segments[duid][r] for r in rms]
                    after: list[dict[str, Any]] = []
                    if mode == "both":
                        # Release the wet pass per room done by the DRY robot; a
                        # both-capable robot additionally waits for its own dry
                        # session (single-room case — no benefit to pooling).
                        after = [
                            {"duid": room_dry_duid[r], "room": r}
                            for r in rms
                            if r in room_dry_duid
                        ]
                        if own_gate:
                            after.append(own_gate)
                    tasks.append(
                        {
                            "id": f"wet{j}",
                            "vacuum": entity,
                            "selects": selects,
                            "fan_speed": fan,
                            "service": "vacuum.send_command",
                            "service_data": {
                                "entity_id": entity,
                                "command": "app_segment_clean",
                                "params": [{"segments": segs, "repeat": repeat}],
                            },
                            "after": after,
                        }
                    )
                plan["wet"][entity] = list(rms)

        if not plan["unassigned"]:
            plan.pop("unassigned")
        plan.update(timeline or self._estimate_timeline(dry_assign, wet_assign))
        return tasks, plan
