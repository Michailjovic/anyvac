"""Coordinator that reads parsed Roborock map data and normalises it.

Design note (piggyback): we deliberately do NOT open our own Roborock
connection. Instead we read the parsed ``MapData`` out of the official Roborock
integration's runtime coordinators:

    roborock config entry
      -> entry.runtime_data.v1            (list of v1 coordinators)
      -> coord.properties_api.home        (HomeTrait)
      -> home.home_map_content[flag]      (MapContent)
      -> map_content.map_data             (vacuum_map_parser MapData)

This couples us to the Roborock integration's internal structure, so every
access is defensive (getattr / try-except) and a missing/changed attribute just
yields no data for that vacuum rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PATH_MAX_POINTS, ROBOROCK_DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

# Grid cell size (mm) for the per-room spatial cleaning-coverage estimate. The path
# is bucketed into cells of this size; coverage = visited cells / cells in the room
# bounding box. ~250 mm ≈ the vacuum's cleaning width, a sensible coverage granularity.
COVERAGE_CELL_MM = 250


@dataclass
class AnyVacDevice:
    """Normalised, card-ready map data for a single vacuum."""

    duid: str
    slug: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)


def _point(p: Any) -> dict[str, float] | None:
    """Convert a parser Point (x, y, optional angle a) to a plain dict."""
    if p is None:
        return None
    x = getattr(p, "x", None)
    y = getattr(p, "y", None)
    if x is None or y is None:
        return None
    out: dict[str, float] = {"x": x, "y": y}
    a = getattr(p, "a", None)
    if a is not None:
        out["a"] = a
    return out


def _decimate(points: list[Any], max_points: int) -> list[Any]:
    """Down-sample a list to at most ``max_points`` entries, keeping shape."""
    n = len(points)
    if n <= max_points or max_points <= 0:
        return points
    step = (n + max_points - 1) // max_points
    return points[::step]


def _room_coverage(
    points: list[dict[str, float]], rooms: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Estimate per-room spatial cleaning coverage from the cleaning path.

    The room bounding box is gridded into ``COVERAGE_CELL_MM`` cells; a cell counts as
    cleaned if any path point falls in it. Coverage % = visited cells / total cells in
    the box. NOTE this is an approximation: the box is rectangular and includes
    furniture / non-floor area, so a fully cleaned room typically plateaus below 100 %.
    Good enough as a live debug signal that the path is being tracked correctly.
    """
    # Keyed by segment_id (always present). Names are merged into the rooms only after
    # _extract_map runs, so keying by name here would skip every room; the caller maps
    # segment_id -> name when consuming the coverage.
    boxes: list[tuple[str, float, float, float, float, int]] = []
    cov: dict[str, dict[str, Any]] = {}
    for r in rooms:
        seg = r.get("segment_id")
        x0, y0, x1, y1 = r.get("x0"), r.get("y0"), r.get("x1"), r.get("y1")
        if seg is None or None in (x0, y0, x1, y1):
            continue
        key = str(seg)
        lx, hx = (x0, x1) if x0 <= x1 else (x1, x0)
        ly, hy = (y0, y1) if y0 <= y1 else (y1, y0)
        ncx = max(1, int((hx - lx) // COVERAGE_CELL_MM) + 1)
        ncy = max(1, int((hy - ly) // COVERAGE_CELL_MM) + 1)
        boxes.append((key, lx, ly, hx, hy, ncx))
        cov[key] = {"_cells": set(), "total": ncx * ncy}
    for p in points:
        x = p.get("x")
        y = p.get("y")
        if x is None or y is None:
            continue
        for nm, lx, ly, hx, hy, _ncx in boxes:
            if lx <= x <= hx and ly <= y <= hy:
                cx = int((x - lx) // COVERAGE_CELL_MM)
                cy = int((y - ly) // COVERAGE_CELL_MM)
                cov[nm]["_cells"].add((cx, cy))
    out: dict[str, dict[str, Any]] = {}
    for nm, d in cov.items():
        visited = len(d["_cells"])
        total = d["total"]
        out[nm] = {
            "visited": visited,
            "total": total,
            "pct": round(100 * visited / total) if total else None,
        }
    return out


def _extract_map(map_data: Any) -> dict[str, Any]:
    """Pull the fields the card needs out of a parser MapData object."""
    out: dict[str, Any] = {}

    out["vacuum_position"] = _point(getattr(map_data, "vacuum_position", None))
    out["charger"] = _point(getattr(map_data, "charger", None))

    # Calibration points (vacuum mm <-> map px), computed by the parser itself.
    try:
        out["calibration_points"] = map_data.calibration()
    except Exception as err:  # noqa: BLE001 - never let calibration break the update
        _LOGGER.debug("AnyVac: calibration() failed: %s", err)
        out["calibration_points"] = None

    # Cleaning path: list[list[Point]] -> flat decimated list of {x, y}.
    flat: list[dict[str, float]] = []
    path_obj = getattr(map_data, "path", None)
    subpaths = getattr(path_obj, "path", None) if path_obj is not None else None
    if subpaths:
        for sub in subpaths:
            for pt in sub:
                px = getattr(pt, "x", None)
                py = getattr(pt, "y", None)
                if px is not None and py is not None:
                    flat.append({"x": px, "y": py})
    out["path"] = _decimate(flat, PATH_MAX_POINTS)
    out["path_points"] = len(flat)  # raw (undecimated) count — grows through the clean

    # Mop (wet) path as a separate layer.
    mflat: list[dict[str, float]] = []
    mop_obj = getattr(map_data, "mop_path", None)
    msub = getattr(mop_obj, "path", None) if mop_obj is not None else None
    if msub:
        for sub in msub:
            for pt in sub:
                mx = getattr(pt, "x", None)
                my = getattr(pt, "y", None)
                if mx is not None and my is not None:
                    mflat.append({"x": mx, "y": my})
    out["mop_path"] = _decimate(mflat, PATH_MAX_POINTS)
    out["mop_path_points"] = len(mflat)

    # Rooms: {segment_number: Room} -> list of plain dicts.
    rooms: list[dict[str, Any]] = []
    room_dict = getattr(map_data, "rooms", None) or {}
    for num, room in room_dict.items():
        rooms.append(
            {
                "segment_id": getattr(room, "number", num),
                "name": getattr(room, "name", None),
                "x0": getattr(room, "x0", None),
                "y0": getattr(room, "y0", None),
                "x1": getattr(room, "x1", None),
                "y1": getattr(room, "y1", None),
                "pos_x": getattr(room, "pos_x", None),
                "pos_y": getattr(room, "pos_y", None),
            }
        )
    out["rooms"] = rooms

    # Per-room spatial coverage (debug / live progress): both dry and wet paths count
    # as "floor covered". Computed from the full (undecimated) trajectory.
    out["rooms_coverage"] = _room_coverage(flat + mflat, rooms)

    # Which segments the robot has cleaned (this session) + where it currently is.
    out["cleaned_rooms"] = sorted(getattr(map_data, "cleaned_rooms", None) or [])
    out["vacuum_room"] = getattr(map_data, "vacuum_room", None)
    out["vacuum_room_name"] = getattr(map_data, "vacuum_room_name", None)

    # Image dimensions (for mm <-> map-pixel conversions on the card side).
    img = getattr(map_data, "image", None)
    dims = getattr(img, "dimensions", None) if img is not None else None
    if dims is not None:
        out["image_dims"] = {
            "top": getattr(dims, "top", None),
            "left": getattr(dims, "left", None),
            "width": getattr(dims, "width", None),
            "height": getattr(dims, "height", None),
            "scale": getattr(dims, "scale", None),
            "rotation": getattr(dims, "rotation", None),
        }

    return out


class AnyVacCoordinator(DataUpdateCoordinator[dict[str, AnyVacDevice]]):
    """Periodically read parsed map data from the Roborock integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._store: Store = Store(hass, 1, f"{DOMAIN}_room_history")
        # Cross-vacuum room history keyed by room NAME: {name: {dry, wet, any: iso}}
        self._history: dict[str, dict[str, str]] = {}
        self._was_cleaning: dict[str, bool] = {}
        self._session_rooms: dict[str, set[str]] = {}
        self._session_start: dict[str, datetime] = {}
        self._est_store: Store = Store(hass, 1, f"{DOMAIN}_room_estimates")
        # Learned clean-time estimates kept PER VACUUM (cleaning speeds differ between
        # models, so they are never shared): {duid: {room_name: {dry: min, wet: min}}}
        self._estimates: dict[str, dict[str, dict[str, float]]] = {}
        # Room-done detection (orchestrator real-time signal): debounce a room change
        # over consecutive polls so a momentary boundary cross does not fire.
        self._raw_room: dict[str, str | None] = {}
        self._raw_count: dict[str, int] = {}
        self._confirmed_room: dict[str, str | None] = {}
        # Rooms the robot was *confirmed* in this session (debounced) — used for the
        # single-room calibration check so a momentary boundary cross does not count.
        self._session_confirmed: dict[str, set[str]] = {}
        # Clean type observed *while actually cleaning a room* this session. The robot
        # resets its settings to default at the end of a clean, so the clean_type read
        # at the finish poll is unreliable — capture it mid-clean instead.
        self._session_clean_type: dict[str, str] = {}
        # Diagnostics: the last single-room calibration decision per vacuum (for the
        # card's Debug tab) — so you can see exactly why an estimate did / didn't write.
        self._last_calib: dict[str, dict[str, Any]] = {}
        # Card-level room selection (which rooms are queued to clean), shared across
        # devices and fed to the orchestrator. Persisted across restarts.
        self._sel_store: Store = Store(hass, 1, f"{DOMAIN}_selection")
        self._selected_rooms: set[str] = set()
        # Live per-room elapsed cleaning seconds this session (for the debug progress
        # gauge's time-ratio) + last poll timestamp to measure the per-poll delta.
        self._room_elapsed: dict[str, dict[str, float]] = {}
        self._last_poll: dict[str, datetime] = {}

    @property
    def rooms_history(self) -> dict[str, dict[str, str]]:
        """Cross-vacuum per-room clean history (by room name)."""
        return self._history

    def all_room_names(self) -> set[str]:
        """All known room names across vacuums (for per-room timestamp sensors)."""
        names: set[str] = set()
        for dev in (self.data or {}).values():
            for r in dev.data.get("rooms", []):
                nm = r.get("name")
                if nm:
                    names.add(nm)
        return names

    @property
    def rooms_estimate(self) -> dict[str, dict[str, dict[str, float]]]:
        """Learned clean-time estimates per vacuum: {duid: {room: {dry, wet}}}."""
        return self._estimates

    @property
    def selected_rooms(self) -> list[str]:
        """Card-level set of rooms queued to clean (shared across devices)."""
        return sorted(self._selected_rooms)

    def set_selection(self, rooms: list[str], mode: str = "set") -> None:
        """Mutate the shared room selection. mode: set / add / remove / toggle / clear."""
        names = {str(r) for r in rooms}
        if mode == "clear":
            self._selected_rooms = set()
        elif mode == "set":
            self._selected_rooms = set(names)
        elif mode == "add":
            self._selected_rooms |= names
        elif mode == "remove":
            self._selected_rooms -= names
        elif mode == "toggle":
            self._selected_rooms ^= names
        self._sel_store.async_delay_save(lambda: sorted(self._selected_rooms), 2)
        self.async_update_listeners()

    def _learn_estimate(
        self, duid: str, room: str, kind: str | None, minutes: float
    ) -> tuple[float | None, float | None]:
        """Update one vacuum's learned estimate for a room+type from a measured
        single-room clean. Estimates are kept PER VACUUM because cleaning speeds differ
        between models — they are never shared across vacuums. Rolling average (first
        sample = the measured value, then weighted). Returns (before, after)."""
        if kind not in ("dry", "wet") or not (1 <= minutes <= 180):
            return None, None
        rec = self._estimates.setdefault(duid, {}).setdefault(room, {})
        before = rec.get(kind)
        after = round(minutes) if before is None else round(0.6 * before + 0.4 * minutes)
        rec[kind] = after
        self._est_store.async_delay_save(lambda: self._estimates, 5)
        return before, after

    def _accumulate_elapsed(self, device: AnyVacDevice) -> None:
        """Add the per-poll time delta to the room the vacuum is currently cleaning, so
        the debug gauge can show a time-ratio (elapsed / learned estimate)."""
        duid = device.duid
        now = dt_util.utcnow()
        last = self._last_poll.get(duid)
        self._last_poll[duid] = now
        if not device.data.get("in_cleaning") or last is None:
            return
        delta = (now - last).total_seconds()
        if delta <= 0 or delta > 600:  # ignore restarts / large gaps
            return
        room = self._confirmed_room.get(duid) or device.data.get("vacuum_room_name")
        if room:
            e = self._room_elapsed.setdefault(duid, {})
            e[room] = e.get(room, 0.0) + delta

    def _detect_room_done(self, device: AnyVacDevice) -> None:
        """Fire anyvac_room_done when a vacuum has truly finished and left a room.

        This is the real-time signal the orchestrator's wet robot waits on. We do NOT
        use the raw current room directly (the robot crosses its own room borders
        mid-clean and briefly reports neighbours); a new room must persist over two
        consecutive polls before it is "confirmed", and the previously confirmed room
        is then reported done. The last room is also reported done on return-to-dock.
        """
        duid = device.duid
        cleaning = bool(device.data.get("in_cleaning"))
        raw = device.data.get("vacuum_room_name")

        if not cleaning:
            prev = self._confirmed_room.get(duid)
            if prev and self._was_cleaning.get(duid):
                self._stamp_history(
                    prev, self._session_clean_type.get(duid) or device.data.get("clean_type")
                )
                self.hass.bus.async_fire(
                    f"{DOMAIN}_room_done",
                    {"vacuum": device.name, "duid": duid, "room": prev, "reason": "docked"},
                )
            self._confirmed_room[duid] = None
            self._raw_room[duid] = None
            self._raw_count[duid] = 0
            return

        if raw == self._raw_room.get(duid):
            self._raw_count[duid] = self._raw_count.get(duid, 0) + 1
        else:
            self._raw_room[duid] = raw
            self._raw_count[duid] = 1

        if raw and self._raw_count[duid] >= 2 and raw != self._confirmed_room.get(duid):
            prev = self._confirmed_room.get(duid)
            self._confirmed_room[duid] = raw
            self._session_confirmed.setdefault(duid, set()).add(raw)
            if prev:
                self._stamp_history(
                    prev, self._session_clean_type.get(duid) or device.data.get("clean_type")
                )
                self.hass.bus.async_fire(
                    f"{DOMAIN}_room_done",
                    {"vacuum": device.name, "duid": duid, "room": prev, "reason": "left"},
                )

    def _track_and_emit(self, device: AnyVacDevice) -> None:
        """Fire anyvac_clean_started / anyvac_clean_finished events on cleaning transitions.

        Notifications are built by the user from these events + the per-room timestamp
        sensors; the integration never composes message text itself.
        """
        duid = device.duid
        cleaning = bool(device.data.get("in_cleaning"))
        was = self._was_cleaning.get(duid, False)
        if cleaning:
            if not was:
                self._session_rooms[duid] = set()
                self._session_confirmed[duid] = set()
                self._session_clean_type.pop(duid, None)
                self._room_elapsed[duid] = {}
                self._session_start[duid] = dt_util.utcnow()
                self.hass.bus.async_fire(
                    f"{DOMAIN}_clean_started",
                    {"vacuum": device.name, "duid": duid, "clean_type": device.data.get("clean_type")},
                )
            room = device.data.get("vacuum_room_name")
            if room:
                self._session_rooms.setdefault(duid, set()).add(room)
                # Capture clean type only while genuinely cleaning a room (settings
                # applied, before the end-of-clean reset to default).
                ct_now = device.data.get("clean_type")
                if ct_now in ("dry", "wet"):
                    self._session_clean_type[duid] = ct_now
        elif was:
            started = self._session_start.get(duid)
            duration_min = round((dt_util.utcnow() - started).total_seconds() / 60) if started else None
            rooms = sorted(self._session_rooms.get(duid, set()))
            # Use the clean type captured *during* the clean, not the finish-poll value
            # (the robot resets to its default mode at the end of a clean).
            ct = self._session_clean_type.get(duid) or device.data.get("clean_type")
            event: dict[str, Any] = {
                "vacuum": device.name,
                "duid": duid,
                "clean_type": ct,
                "rooms": rooms,
                "duration_min": duration_min,
            }
            # A single-room clean is a clean calibration sample: learn that room+type's
            # real duration and report the change (before -> after) on the event so a
            # notification can say "estimate went from X to Y". Use the *confirmed*
            # (debounced) rooms, not every transiently reported one, so a brief boundary
            # cross into a neighbour does not disqualify a genuine single-room clean.
            confirmed = sorted(self._session_confirmed.get(duid, set()))
            wrote = False
            before: float | None = None
            after: float | None = None
            if len(confirmed) == 1 and duration_min is not None:
                before, after = self._learn_estimate(duid, confirmed[0], ct, duration_min)
                if after is not None:
                    wrote = True
                    event["calibrated_room"] = confirmed[0]
                    event["estimate_before"] = before
                    event["estimate_after"] = after
            self._last_calib[duid] = {
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "confirmed_rooms": confirmed,
                "clean_type": ct,
                "duration_min": duration_min,
                "wrote": wrote,
                "before": before,
                "after": after,
                "reason": (
                    "ok"
                    if wrote
                    else f"{len(confirmed)} confirmed rooms (need exactly 1)"
                    if len(confirmed) != 1
                    else "clean_type not dry/wet"
                    if ct not in ("dry", "wet")
                    else "duration out of 1-180 min range"
                    if not (duration_min and 1 <= duration_min <= 180)
                    else "rejected"
                ),
            }
            self.hass.bus.async_fire(f"{DOMAIN}_clean_finished", event)
            self._session_rooms[duid] = set()
        self._was_cleaning[duid] = cleaning

    async def _async_setup(self) -> None:
        """Load persisted per-room clean history before the first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._history = {k: dict(v) for k, v in stored.items() if isinstance(v, dict)}
        sel = await self._sel_store.async_load()
        if isinstance(sel, list):
            self._selected_rooms = {str(r) for r in sel}
        est = await self._est_store.async_load()
        if isinstance(est, dict):
            loaded: dict[str, dict[str, dict[str, float]]] = {}
            for duid, rooms in est.items():
                if not isinstance(rooms, dict):
                    continue
                for room, kinds in rooms.items():
                    if not isinstance(kinds, dict):
                        continue
                    vals = {k: float(v) for k, v in kinds.items() if isinstance(v, (int, float))}
                    if vals:
                        loaded.setdefault(duid, {})[room] = vals
            self._estimates = loaded

    def _history_for_save(self) -> dict[str, dict[str, str]]:
        return self._history

    def _stamp_history(self, name: str | None, ctype: str | None) -> None:
        """Record that `name` was cleaned now, with the given clean type, keyed by room
        NAME so history aggregates across all vacuums. Persisted across restarts."""
        if not name:
            return
        now = dt_util.utcnow().isoformat()
        rec = self._history.setdefault(name, {})
        rec["any"] = now
        if ctype in ("dry", "wet"):
            rec[ctype] = now
        self._store.async_delay_save(self._history_for_save, 5)

    def _update_history(self, device: AnyVacDevice) -> None:
        """Stamp rooms the firmware explicitly reports as cleaned (cleaned_rooms).

        Presence-based stamping (the room the robot is currently in) is intentionally
        NOT done here: the robot crosses its own room borders and drives through rooms
        on its way elsewhere, so the raw current room over-reports. Presence is instead
        recorded by the DEBOUNCED confirmation in _detect_room_done (a room must be
        genuinely cleaned and then left before it is stamped), the same signal as
        anyvac_room_done. cleaned_rooms, when the firmware provides it, lists only
        actually-cleaned segments, so it is trustworthy and unioned in here.
        """
        if not device.data.get("in_cleaning"):
            return
        seg_to_name = {
            str(r.get("segment_id")): r.get("name") for r in device.data.get("rooms", [])
        }
        ctype = self._session_clean_type.get(device.duid) or device.data.get("clean_type")
        for seg in device.data.get("cleaned_rooms") or []:
            self._stamp_history(seg_to_name.get(str(seg)), ctype)

    def _build_progress(self, device: AnyVacDevice) -> dict[str, dict[str, Any]]:
        """Merge per-room spatial coverage with the time-ratio into one debug payload:
        {room: {spatial_pct, visited_cells, total_cells, time_pct, elapsed_s, est_s}}."""
        duid = device.duid
        # rooms_coverage is keyed by segment_id; map it onto room names (now merged in).
        cov_by_seg = device.data.get("rooms_coverage") or {}
        seg_to_name = {
            str(r.get("segment_id")): r.get("name") for r in device.data.get("rooms", [])
        }
        cov: dict[str, Any] = {}
        for seg, c in cov_by_seg.items():
            nm = seg_to_name.get(str(seg))
            if nm:
                cov[nm] = c
        elapsed = self._room_elapsed.get(duid, {})
        ests = self._estimates.get(duid, {})
        ctype = self._session_clean_type.get(duid) or device.data.get("clean_type")
        out: dict[str, dict[str, Any]] = {}
        for nm in set(cov) | set(elapsed):
            c = cov.get(nm) or {}
            el = elapsed.get(nm)
            er = ests.get(nm) or {}
            est_min = er.get(ctype) if ctype in ("dry", "wet") else None
            if est_min is None:
                est_min = er.get("dry") or er.get("wet")
            est_s = round(est_min * 60) if est_min else None
            time_pct = None
            if el is not None and est_s:
                time_pct = min(round(100 * el / est_s), 999)
            out[nm] = {
                "spatial_pct": c.get("pct"),
                "visited_cells": c.get("visited"),
                "total_cells": c.get("total"),
                "elapsed_s": round(el) if el is not None else None,
                "est_s": est_s,
                "time_pct": time_pct,
            }
        return out

    async def _async_update_data(self) -> dict[str, AnyVacDevice]:
        """Read every Roborock v1 coordinator and normalise its map data."""
        result: dict[str, AnyVacDevice] = {}
        for rb_entry in self.hass.config_entries.async_entries(ROBOROCK_DOMAIN):
            runtime = getattr(rb_entry, "runtime_data", None)
            coords = getattr(runtime, "v1", None) or []
            for coord in coords:
                try:
                    device = self._extract_device(coord)
                except Exception as err:  # noqa: BLE001 - one bad device must not fail the rest
                    _LOGGER.debug("AnyVac: failed reading a Roborock coordinator: %s", err)
                    continue
                if device is not None:
                    self._update_history(device)
                    self._detect_room_done(device)
                    self._track_and_emit(device)
                    self._accumulate_elapsed(device)
                    progress = self._build_progress(device)
                    for room in device.data.get("rooms", []):
                        rec = self._history.get(room.get("name")) or {}
                        room["last_cleaned"] = rec.get("any")
                        room["last_dry"] = rec.get("dry")
                        room["last_wet"] = rec.get("wet")
                        est = (self._estimates.get(device.duid) or {}).get(room.get("name")) or {}
                        room["estimate_dry"] = est.get("dry")
                        room["estimate_wet"] = est.get("wet")
                        room["progress_pct"] = (progress.get(room.get("name")) or {}).get("spatial_pct")
                    device.data["rooms_last_cleaned"] = {k: dict(v) for k, v in self._history.items()}
                    device.data["rooms_estimate"] = {
                        r: dict(c) for r, c in (self._estimates.get(device.duid) or {}).items()
                    }
                    device.data["rooms_progress"] = progress
                    device.data.pop("rooms_coverage", None)
                    device.data["duid"] = device.duid
                    device.data["calib_debug"] = self._last_calib.get(device.duid)
                    result[device.duid] = device
        return result

    def _extract_device(self, coord: Any) -> AnyVacDevice | None:
        """Extract normalised map data for one Roborock v1 coordinator."""
        home = getattr(getattr(coord, "properties_api", None), "home", None)
        if home is None:
            return None

        contents = getattr(home, "home_map_content", None) or {}
        # Prefer the current map; fall back to any cached map content.
        current = getattr(home, "current_map_data", None)
        flag = getattr(current, "map_flag", None)
        map_content = contents.get(flag) if flag is not None else None
        if map_content is None:
            map_content = next(iter(contents.values()), None)

        map_data = getattr(map_content, "map_data", None) if map_content else None
        if map_data is None:
            return None

        data = _extract_map(map_data)

        # MapData.rooms carry no names; merge them from the home trait's room mapping.
        names: dict[int, str] = {}
        try:
            for room_map in getattr(home, "current_rooms", None) or []:
                seg = getattr(room_map, "segment_id", None)
                nm = getattr(room_map, "name", None)
                if seg is not None and nm:
                    names[seg] = nm
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("AnyVac: failed reading room names: %s", err)
        for room in data.get("rooms", []):
            if room.get("name") is None and room.get("segment_id") in names:
                room["name"] = names[room["segment_id"]]

        status = getattr(getattr(coord, "properties_api", None), "status", None)

        def _s(attr: str) -> Any:
            return getattr(status, attr, None) if status is not None else None

        data["in_cleaning"] = bool(_s("in_cleaning"))
        water_name = _s("water_mode_name")
        data["mop_signal"] = {
            "fan_power": _s("fan_power"),
            "fan_speed_name": _s("fan_speed_name"),
            "water_box_mode": _s("water_box_mode"),
            "water_mode_name": water_name,
            "mop_mode": _s("mop_mode"),
            "mop_route_name": _s("mop_route_name"),
            "water_box_status": _s("water_box_status"),
            "water_box_carriage_status": _s("water_box_carriage_status"),
            "is_water_box_carriage_attached": _s("is_water_box_carriage_attached"),
        }
        # Best-effort dry/wet: "wet" when a water level is active. Verify via mop_signal
        # and we will refine the exact field once confirmed on hardware.
        data["clean_type"] = (
            "wet" if water_name and str(water_name).lower() not in ("off", "none", "closed") else "dry"
        )
        vr = data.get("vacuum_room")
        if vr is not None and not data.get("vacuum_room_name"):
            data["vacuum_room_name"] = names.get(vr)

        duid = getattr(coord, "duid", None) or "unknown"
        slug = getattr(coord, "duid_slug", None) or duid
        name = getattr(getattr(coord, "device", None), "name", None) or slug
        return AnyVacDevice(duid=duid, slug=slug, name=name, data=data)
