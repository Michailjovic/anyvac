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

import hashlib
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import Any

import numpy as np

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import coverage as covmod
from . import homeframe, livediff, localchannel
from . import reach as reachmod
from .const import (
    DEFAULT_EXPOSE_LEGACY_MM,
    DEFAULT_LIVE_DIFF,
    DOMAIN,
    LIVE_DIFF_BACKOFF_S,
    LIVE_DIFF_INTERVAL_S,
    LIVE_DIFF_MAX_FAILS,
    LIVE_DIFF_NODIFF_LIMIT,
    LIVE_DIFF_TIMEOUT_S,
    OPT_EXPOSE_LEGACY_MM,
    OPT_LIVE_DIFF,
    PATH_MAX_POINTS,
    ROBOROCK_DOMAIN,
    SCAN_INTERVAL_SECONDS,
    SERVICE_POINT_NAME_MAX,
    SERVICE_POINTS_MAX,
)

_LOGGER = logging.getLogger(__name__)

# docs/52 §2, HW 2026-11-25: ``in_cleaning`` stays on while the robot drives
# home after a finished task and while the dock washes the mop at its end
# (S8 19:29 -> 19:48:52 over two chained rooms). The end of a TASK is the
# state edge from active cleaning to ``returning_home`` — unless the battery is
# this low, which is the firmware's own trip home to recharge mid-task
# (it returns at 20 %).
LOW_BATTERY_RETURN_PCT = 25
# How long the app room order read from the robot stays valid (docs/52 O1).
APP_SEQUENCE_TTL_S = 3600

# Raw Roborock states in which the robot is driving somewhere / servicing itself rather
# than cleaning the room it is physically in. CRITICAL (docs/13 A1+A2, docs/14 rule 4):
# a mid-clean mop wash keeps ``in_cleaning`` truthy while HA maps the vacuum state to
# ``docked`` — so room confirmation, per-room elapsed time and coverage attribution must
# all FREEZE during these states, otherwise the dock's room gets "confirmed", rooms fire
# ``anyvac_room_done`` prematurely and single-room calibration never sees exactly 1 room.
# Raw Roborock states that mean "the dock is washing the mop right now" — the same
# pair HA 2026.9's `switch.<vacuum>_mop_washing` uses for its `is_on`.
WASH_STATES = {"washing_the_mop", "washing_the_mop_2"}


def _clean_sequence_segments(resp: Any) -> list[int]:
    """Segment ids of a `get_clean_sequence` answer, in order. The firmware
    answers a flat list (``[16, 19, 17]``); tolerate one level of wrapping."""
    if isinstance(resp, dict):
        resp = resp.get("result", resp.get("clean_sequence"))
    if isinstance(resp, (list, tuple)) and len(resp) == 1 and isinstance(resp[0], (list, tuple)):
        resp = resp[0]
    out: list[int] = []
    for v in resp if isinstance(resp, (list, tuple)) else []:
        if isinstance(v, (int, float)) and int(v) > 0:
            out.append(int(v))
    return out


def _mode_is_off(name: Any) -> bool:
    """Is a Roborock fan/water mode name the "not running" one?

    python-roborock 7.x (HA 2026.9) stopped offering ``VacuumModes.OFF_RAISE_MAIN_BRUSH``
    (code 109) in ``fan_speed_options`` for pure-clean-mop devices that can raise the
    main brush — they now get plain ``OFF`` (105, "off"). On 5.x such a device reported
    ``fan_speed_name == "off_raise_main_brush"``, which a plain membership test does NOT
    recognise as off — so a mop-only pass counted as vacuuming and painted the dry layer
    (the docs/16 bug class). Prefix-matching "off" is correct on BOTH library versions;
    do not narrow this back to an exact-match set.
    """
    if name is None:
        return False
    n = str(name).strip().lower()
    return n.startswith("off") or n in ("none", "closed")


# Raw Roborock states in which the robot is driving AND cleaning (docs/47 §1.5).
# A poll that sees no new map snapshot in one of these states defers its time to
# the points the next snapshot brings; in any other state (paused, idle, transit,
# unknown) the time is dropped exactly as before docs/47. A positive list on
# purpose: an unknown/new firmware state falls back to the old behaviour.
# 1.51.6 (plan po auditu, A0): how many full polls `clean_type_trace` keeps
# before and after a clean's falling edge, and how many of AnyVac's own clean
# events it remembers.
_CT_TRACE_BEFORE = 8
_CT_TRACE_AFTER = 3
_CT_TRACE_EVENTS = 6

ACTIVE_CLEAN_STATES = {
    "cleaning",
    "spot_cleaning",
    "zoned_cleaning",
    "segment_cleaning",
    "robot_status_mopping",
    "clean_mop_cleaning",
    "clean_mop_mopping",
    "segment_mopping",
    "segment_clean_mop_cleaning",
    "segment_clean_mop_mopping",
    "zoned_mopping",
    "zoned_clean_mop_cleaning",
    "zoned_clean_mop_mopping",
}


TRANSIT_STATES = {
    "returning_home",
    "docking",
    "going_to_target",
    "emptying_the_bin",
    "washing_the_mop",
    "washing_the_mop_2",
    "going_to_wash_the_mop",
    "back_to_dock_washing_duster",
    "air_drying",
    "air_drying_stopping",
    "charging",
    "charging_complete",
}


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


# Ramer-Douglas-Peucker tolerance, in mm — a point is dropped only if it's within
# this distance of the straight line between its kept neighbours. Well under a
# room-scale feature and under the robot's own positioning precision, so this
# never causes a visible shape change; it just removes points that were adding
# no information (near-collinear runs). Fixed, not derived from max_points: the
# goal is "don't distort geometry", not "hit an exact point count" — max_points
# below is a separate hard safety cap for pathological (very long) sessions.
_RDP_EPSILON_MM = 20.0
# Guard against RDP's rare worst-case (near O(n^2) for adversarially-shaped
# input): pre-thin anything absurdly large with a cheap stride pass first, so
# RDP itself never has to chew through more than this many points at once.
_RDP_PRETHIN_MAX = 4000

# Debounce (seconds) for the two `_paths_store` write paths (1.1.0).
#
# The store is a full-file rewrite, so at the old 5 s debounce a 30 s poll meant
# one whole-file write per poll for the entire clean. Persistence exists so a HA
# restart doesn't blank the last trace — it does NOT need 30 s granularity, and
# losing the last minute of a trace to an unclean shutdown is a non-event (the
# next poll re-reads it from the robot's own buffer anyway). Raised to 2 min for
# incremental appends.
#
# A wipe keeps the short delay: it must land promptly, because until it does the
# on-disk copy still holds the PREVIOUS job's trace, and a restart in that window
# would restore it as if it belonged to the new job (`_sortie_is_new_job` has
# already fired by then and won't fire again for the same job).
_PATHS_SAVE_DELAY_S = 120
_PATHS_WIPE_SAVE_DELAY_S = 5


def _rdp_simplify(points: list[dict[str, float]], epsilon: float) -> list[dict[str, float]]:
    """Ramer-Douglas-Peucker trajectory simplification, iterative (stack-based,
    so a long straight run can't blow the recursion limit). Keeps a point only
    if it deviates from the straight line between its surviving neighbours by
    more than ``epsilon`` — unlike naive every-Nth-point stride decimation, this
    barely touches straight sweeps (which need almost no points to represent
    exactly) and preserves turns (where real information/curvature lives)."""
    n = len(points)
    if n < 3:
        return points
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    eps2 = epsilon * epsilon
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        p0, p1 = points[start], points[end]
        dx, dy = p1["x"] - p0["x"], p1["y"] - p0["y"]
        seg_len2 = dx * dx + dy * dy
        max_dist2 = -1.0
        max_idx = start
        for i in range(start + 1, end):
            p = points[i]
            if seg_len2 > 0:
                t = ((p["x"] - p0["x"]) * dx + (p["y"] - p0["y"]) * dy) / seg_len2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                projx, projy = p0["x"] + t * dx, p0["y"] + t * dy
            else:
                projx, projy = p0["x"], p0["y"]
            ddx, ddy = p["x"] - projx, p["y"] - projy
            dist2 = ddx * ddx + ddy * ddy
            if dist2 > max_dist2:
                max_dist2 = dist2
                max_idx = i
        if max_dist2 > eps2:
            keep[max_idx] = 1
            stack.append((start, max_idx))
            stack.append((max_idx, end))
    return [p for p, k in zip(points, keep) if k]


def _simplify(points: list[Any]) -> list[Any]:
    """The expensive, budget-INDEPENDENT half of ``_decimate``: optional stride
    pre-thin (RDP worst-case guard) followed by RDP itself. Split out in 1.1.0
    purely so it can be memoised — its result depends only on the point list,
    unlike the cap below, which depends on how big the rest of the trace is."""
    n = len(points)
    pre = points[:: max(1, n // _RDP_PRETHIN_MAX)] if n > _RDP_PRETHIN_MAX else points
    return _rdp_simplify(pre, _RDP_EPSILON_MM)


def _cap(simplified: list[Any], max_points: int) -> list[Any]:
    """The cheap, budget-DEPENDENT half: a last-resort uniform stride so a
    pathological (very long / multi-hour) session still can't exceed the cap.
    In the common case RDP alone lands under it and this is a no-op."""
    m = len(simplified)
    if m <= max_points:
        return simplified
    step = (m + max_points - 1) // max_points
    return simplified[::step]


def _decimate(points: list[Any], max_points: int) -> list[Any]:
    """Down-sample a list to at most ``max_points`` entries, preserving shape.

    Shape-preserving RDP simplification (see ``_rdp_simplify``) does the real
    work; ``max_points`` is a hard safety cap for pathological cases, applied as
    a last-resort uniform stride on top of the already-simplified points.
    """
    if len(points) <= max_points or max_points <= 0:
        return points
    return _cap(_simplify(points), max_points)


def _decimate_segments(
    segments: list[list[Any]],
    max_points: int,
    cache: dict[int, tuple[int, list[Any]]] | None = None,
) -> list[list[Any]]:
    """Down-sample each segment independently, preserving the segment breaks.

    Budget is split across segments proportional to their length so long segments
    keep more detail. Never merges/reorders across segments — that would recreate
    the exact straight-line-across-a-gap bug this segmentation exists to avoid.

    ``cache`` (1.1.0) memoises the expensive ``_simplify`` (RDP) step per segment
    INDEX, keyed on that segment's point count. Only the last segment of a live
    trace ever grows; every earlier one is closed and can never change again, yet
    this ran RDP over all of them from scratch on every 30 s poll, so the cost
    grew with session length rather than with new data.

    Only the RDP result is cached, deliberately NOT the finished output: the
    ``budget`` below is derived from ``total``, which shifts every time the last
    segment grows, so a closed segment's budget drifts even while the segment
    itself is untouched. Caching the finished output under a key that includes
    the budget would therefore miss on essentially every poll — the memo would
    be dead weight. Splitting at ``_simplify``/``_cap`` puts the whole cost on
    the cacheable side and leaves only an O(n) stride outside it.

    A hit is provably identical to a recompute: points are only ever APPENDED to
    a segment (a wipe replaces the whole list and resets `path_seen` with it), so
    equal length means equal content. Callers that don't benefit (one-shot
    conversions) simply pass no cache.
    """
    total = sum(len(s) for s in segments)
    if total <= max_points or max_points <= 0:
        return segments
    out: list[list[Any]] = []
    for i, seg in enumerate(segments):
        if not seg:
            continue
        budget = max(1, round(len(seg) / total * max_points))
        # Mirrors `_decimate`'s early return exactly, so caching changes nothing
        # about what a caller sees: a segment already under its budget is passed
        # through untouched rather than simplified.
        if len(seg) <= budget:
            out.append(seg)
            continue
        hit = cache.get(i) if cache is not None else None
        if hit is not None and hit[0] == len(seg):
            simplified = hit[1]
        else:
            simplified = _simplify(seg)
            if cache is not None:
                cache[i] = (len(seg), simplified)
        out.append(_cap(simplified, budget))
    return out


def _solve_affine(calib: Any) -> tuple[float, float, float, float, float, float] | None:
    """Solve the 2D affine transform mm→px from the parser's calibration points.

    Kontrakt v2 (docs/14 §3.6): mm never leave the backend. The parser gives ≥3
    (vacuum mm ↔ rendered-image px) pairs; solving `u = a·x + b·y + c`,
    `v = d·x + e·y + f` from the first three yields the exact transform the card
    used to compute client-side. Returns (a, b, c, d, e, f) or None.
    """

    def _xy(o: Any) -> tuple[float, float] | None:
        if o is None:
            return None
        x = o.get("x") if isinstance(o, dict) else getattr(o, "x", None)
        y = o.get("y") if isinstance(o, dict) else getattr(o, "y", None)
        if x is None or y is None:
            return None
        return float(x), float(y)

    pts: list[tuple[float, float, float, float]] = []
    for cp in calib or []:
        v = cp.get("vacuum") if isinstance(cp, dict) else getattr(cp, "vacuum", None)
        m = cp.get("map") if isinstance(cp, dict) else getattr(cp, "map", None)
        vxy, mxy = _xy(v), _xy(m)
        if vxy is None or mxy is None:
            continue
        pts.append((vxy[0], vxy[1], mxy[0], mxy[1]))
        if len(pts) == 3:
            break
    if len(pts) < 3:
        return None
    (x1, y1, u1, v1), (x2, y2, u2, v2), (x3, y3, u3, v3) = pts
    det = x1 * (y2 - y3) - y1 * (x2 - x3) + (x2 * y3 - x3 * y2)
    if abs(det) < 1e-9:
        return None

    def _cramer(r1: float, r2: float, r3: float) -> tuple[float, float, float]:
        a = (r1 * (y2 - y3) - y1 * (r2 - r3) + (r2 * y3 - r3 * y2)) / det
        b = (x1 * (r2 - r3) - r1 * (x2 - x3) + (x2 * r3 - x3 * r2)) / det
        c = (x1 * (y2 * r3 - y3 * r2) - y1 * (x2 * r3 - x3 * r2) + r1 * (x2 * y3 - x3 * y2)) / det
        return a, b, c

    a, b, c = _cramer(u1, u2, u3)
    d, e, f = _cramer(v1, v2, v3)
    return (a, b, c, d, e, f)


# docs/42 §9 (fáze pre-H) — defensive re-validation of a stored appearance
# override on load, mirroring `_FLOORPLAN_SEAT_APPEARANCE_SCHEMA` in
# services.py (same duplication posture the `map` fields above already
# have: the schema validates on the way IN via the service call, this
# re-validates on the way OUT of the store in case the on-disk JSON was
# hand-edited or written by an older/different version). Unknown keys and
# invalid values are silently dropped field-by-field, never crashing
# `_async_setup` — an empty result means "nothing survived", which the
# caller treats as "no appearance override".
_APPEARANCE_BLEND_VALUES = ("normal", "lighten", "screen", "plus-lighter")


def _parse_stored_appearance(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def _bool(key: str) -> None:
        v = raw.get(key)
        if isinstance(v, bool):
            out[key] = v

    def _ranged_float(key: str, lo: float, hi: float) -> None:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        v = float(v)
        if math.isfinite(v) and lo <= v <= hi:
            out[key] = v

    def _nullable_str(key: str) -> None:
        if key not in raw:
            return
        v = raw.get(key)
        if v is None or isinstance(v, str):
            out[key] = v

    _bool("hide_map")
    _ranged_float("overlay_opacity", 0, 100)
    blend = raw.get("overlay_blend")
    if blend in _APPEARANCE_BLEND_VALUES:
        out["overlay_blend"] = blend
    _nullable_str("path_color")
    _ranged_float("path_width", 20, 300)
    _nullable_str("mop_path_color")
    _ranged_float("mop_band_opacity", 0, 100)
    _ranged_float("mop_band_width", 20, 400)
    _bool("robot_image_on_map")
    _ranged_float("robot_size", 40, 220)
    _ranged_float("robot_image_rotation", -180, 180)
    return out


# docs/42 §4.4/§8 bod 1 (fáze K) — defensive re-validation of a stored room
# override on load, mirroring `_FLOORPLAN_SEAT_ROOM_SCHEMA` in services.py,
# same posture as `_parse_stored_appearance` above: the schema validates on
# the way IN via the service call, this re-validates on the way OUT of the
# store. Unknown keys and invalid values are silently dropped field-by-field;
# an empty result means "nothing survived for this room".
def _parse_stored_room(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def _finite(key: str) -> None:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        v = float(v)
        if math.isfinite(v):
            out[key] = v

    def _positive_finite(key: str) -> None:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        v = float(v)
        if math.isfinite(v) and v > 0:
            out[key] = v

    _finite("map_x")
    _finite("map_y")
    _positive_finite("map_w")
    _positive_finite("map_h")
    area_id = raw.get("area_id")
    if area_id is None or isinstance(area_id, str):
        if "area_id" in raw:
            out["area_id"] = area_id
    return out


def _parse_stored_rooms(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Re-validate a stored per-vacuum `rooms` map (room_key -> room dict),
    dropping any room_key whose stored value isn't a dict, and any room_key
    whose validated fields all failed re-validation (nothing left to keep)."""
    out: dict[str, dict[str, Any]] = {}
    for room_key, room_raw in raw.items():
        if not isinstance(room_raw, dict):
            continue
        parsed = _parse_stored_room(room_raw)
        if parsed:
            out[str(room_key)] = parsed
    return out


# docs/42 §3.3/§9 (fáze I addendum) — defensive re-validation of the stored
# card-level `room_style` override, same posture as `_parse_stored_appearance`/
# `_parse_stored_room` above (schema validates on the way IN, this re-validates
# on the way OUT of the store).
def _parse_stored_room_style(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def _ranged_float(key: str) -> None:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        v = float(v)
        if math.isfinite(v) and 0 <= v <= 12:
            out[key] = v

    _ranged_float("border_normal")
    _ranged_float("border_selected")
    return out


def _merge_room_overrides(
    existing: dict[str, dict[str, Any]] | None, rooms: dict[str, dict[str, Any] | None]
) -> dict[str, dict[str, Any]]:
    """Apply one `set_floorplan_seat(rooms=...)` call's per-room_key merge
    onto an existing stored `rooms` map — shared by both the per-vacuum
    branch (split mode) and the card-level branch (merged mode) of
    `AnyVacCoordinator.set_floorplan_seat`, since the merge semantics
    (docs/42 §4.4, fáze K) are identical either way: a dict value sets/
    replaces that one room's override, ``None`` clears just that one room's
    override, and any room_key not mentioned in `rooms` is left untouched.
    Values are already validated by SET_FLOORPLAN_SEAT_SCHEMA's
    `_FLOORPLAN_SEAT_ROOMS_SCHEMA` before this is called."""
    stored_rooms: dict[str, dict[str, Any]] = dict(existing or {})
    for room_key, room_val in rooms.items():
        room_key = str(room_key)
        if room_val is None:
            stored_rooms.pop(room_key, None)
            continue
        rounded_room: dict[str, Any] = {}
        for geo_key in ("map_x", "map_y", "map_w", "map_h"):
            if room_val.get(geo_key) is not None:
                rounded_room[geo_key] = round(float(room_val[geo_key]), 2)
        if "area_id" in room_val:
            rounded_room["area_id"] = room_val["area_id"]
        stored_rooms[room_key] = rounded_room
    return stored_rooms


def _px_point(
    p: dict[str, float] | None, aff: tuple[float, float, float, float, float, float]
) -> dict[str, float] | None:
    """Transform one {x, y[, a]} mm point into rendered-image px space. The angle
    ``a`` is passed through unchanged (it is a heading, not a coordinate)."""
    if p is None:
        return None
    x, y = p.get("x"), p.get("y")
    if x is None or y is None:
        return None
    a, b, c, d, e, f = aff
    out: dict[str, float] = {
        "x": round(a * x + b * y + c, 1),
        "y": round(d * x + e * y + f, 1),
    }
    if p.get("a") is not None:
        out["a"] = p["a"]
    return out


def _home_px_heading(a_deg: float, rec: dict[str, Any], frame: dict[str, Any]) -> float:
    """Transform a heading (degrees, robot-mm convention — same space
    `vacuum_position`'s `x`/`y` are in) into the home frame's own PX-space
    heading convention (standard `atan2`: 0° = +x/right, increasing toward
    +y/down — i.e. the raw image-pixel convention, no extra sign flip
    needed by whoever draws it, unlike the legacy per-vacuum `*_px` contract
    where the card itself negates `sin` to undo a flip baked into that
    contract's own solved affine).

    Found 2026-09-14 while wiring the card's home-frame renderer: the FIRST
    real consumer of `vacuum_position_home_px.a` — nothing published it
    correctly before, because nothing had ever needed to read it. A naive
    passthrough of `a_deg` (what shipped in 1.7.0 by mistake) is only
    correct at `rot_deg == 0`; any vacuum registered at 90/180/270°
    (`robot_mm_to_frame_mm`'s own rotation, docs/40 §4.2) would draw its
    marker facing the wrong way once anything actually read this field.

    Deliberately reuses the exact same `robot_mm_to_frame_mm` +
    `mm_to_home_px` pipeline `_home_px_point` uses for positions — two
    points 1mm apart along the heading, transformed exactly like any other
    coordinate pair — rather than re-deriving the rotation/axis-convention
    algebra by hand a second time (docs/14 rule 1): whatever rotation or
    axis convention those already-tested functions apply to a position
    applies identically here, by construction, with no separate formula to
    get wrong.
    """
    rad = math.radians(a_deg)
    x0, y0 = homeframe.robot_mm_to_frame_mm(rec["rot_deg"], rec["tx_mm"], rec["ty_mm"], 0.0, 0.0)
    x1, y1 = homeframe.robot_mm_to_frame_mm(
        rec["rot_deg"], rec["tx_mm"], rec["ty_mm"], math.cos(rad), math.sin(rad)
    )
    cell_mm = frame.get("cell_mm", homeframe.FRAME_CELL_MM)
    scale = frame.get("scale", homeframe.HOME_PX_SCALE)
    px0, py0 = homeframe.mm_to_home_px(frame["origin_mm"], cell_mm, scale, x0, y0)
    px1, py1 = homeframe.mm_to_home_px(frame["origin_mm"], cell_mm, scale, x1, y1)
    return math.degrees(math.atan2(py1 - py0, px1 - px0)) % 360


def _home_px_point(
    p: dict[str, float] | None, rec: dict[str, Any] | None, frame: dict[str, Any] | None
) -> dict[str, float] | None:
    """Transform one {x, y[, a]} point in THIS VACUUM'S OWN mm space into
    published `*_home_px` space (docs/40 §4.2 point 6, kontrakt v3) — the
    stored registration `rec` maps robot mm -> frame mm
    (`homeframe.robot_mm_to_frame_mm`), then the frame's own origin/cell/
    scale maps frame mm -> home px (`homeframe.mm_to_home_px`). Mirrors
    `_px_point`'s shape/rounding so the card treats both contracts
    identically EXCEPT for `a` (see `_home_px_heading` — home-px heading is
    its own self-consistent convention, not a straight copy of the mm-space
    value). None whenever any input is missing (no home-frame registration
    yet for this duid)."""
    if p is None or rec is None or frame is None:
        return None
    x, y = p.get("x"), p.get("y")
    if x is None or y is None:
        return None
    fx_mm, fy_mm = homeframe.robot_mm_to_frame_mm(rec["rot_deg"], rec["tx_mm"], rec["ty_mm"], x, y)
    px, py = homeframe.mm_to_home_px(
        frame["origin_mm"],
        frame.get("cell_mm", homeframe.FRAME_CELL_MM),
        frame.get("scale", homeframe.HOME_PX_SCALE),
        fx_mm,
        fy_mm,
    )
    out: dict[str, float] = {"x": round(px, 1), "y": round(py, 1)}
    if p.get("a") is not None:
        out["a"] = round(_home_px_heading(p["a"], rec, frame), 1)
    return out


def _parse_service_points(stored: Any) -> dict[str, list[dict[str, Any]]]:
    """Load `{duid: [{name, x, y}]}` defensively — a malformed entry is dropped,
    never the whole store (same posture as the other stores)."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(stored, dict):
        return out
    for duid, points in stored.items():
        if not isinstance(points, list):
            continue
        kept: list[dict[str, Any]] = []
        for p in points:
            try:
                name = str(p["name"]).strip()
                x, y = float(p["x"]), float(p["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if not name or not (math.isfinite(x) and math.isfinite(y)):
                continue
            if any(k["name"] == name for k in kept) or len(kept) >= SERVICE_POINTS_MAX:
                continue
            kept.append({"name": name[:SERVICE_POINT_NAME_MAX], "x": int(round(x)), "y": int(round(y))})
        if kept:
            out[str(duid)] = kept
    return out


def _room_at(rooms: list[dict[str, Any]], x: float, y: float) -> str | None:
    """Name of the room whose mm bbox holds (x, y); the smallest one when
    bboxes overlap (a bbox is coarser than the room's real shape)."""
    best: tuple[float, str] | None = None
    for r in rooms:
        try:
            x0, x1 = sorted((float(r["x0"]), float(r["x1"])))
            y0, y1 = sorted((float(r["y0"]), float(r["y1"])))
        except (KeyError, TypeError, ValueError):
            continue
        if x0 <= x <= x1 and y0 <= y <= y1 and r.get("name"):
            area = (x1 - x0) * (y1 - y0)
            if best is None or area < best[0]:
                best = (area, r["name"])
    return best[1] if best else None


def _frame_mm_to_home_px(fx_mm: float, fy_mm: float, frame: dict[str, Any]) -> list[float]:
    """Frame-space mm (e.g. an `outline_from_mask` point, already offset by
    the frame's own `origin_mm`) -> published home px `[x, y]` — no
    robot-space registration step, unlike `_home_px_point` above."""
    px, py = homeframe.mm_to_home_px(
        frame["origin_mm"],
        frame.get("cell_mm", homeframe.FRAME_CELL_MM),
        frame.get("scale", homeframe.HOME_PX_SCALE),
        fx_mm,
        fy_mm,
    )
    return [round(px, 1), round(py, 1)]


def _apply_home_frame_contract(
    device: AnyVacDevice,
    path_dry: list[list[dict[str, float]]],
    path_wet: list[list[dict[str, float]]],
    home_frames: dict[str, dict[str, Any]],
    robot_frame: dict[str, str],
    room_masks: dict[str, dict[str, dict[int, Any]]],
) -> None:
    """Kontrakt v3 (docs/40 §4.2 point 6, additive — every `*_px` attribute
    stays untouched): the SAME points already on `device.data`, re-expressed
    in the shared home frame's own px space, for a vacuum that has a
    home-frame registration. Mutates `device.data`/`device.data["rooms"]` in
    place, mirroring the v2 `*_px` block's own style. Factored out of
    `_async_update_data` (a plain function, not a method) purely so it can be
    unit-tested directly against a hand-built `AnyVacDevice` + frame table,
    without the full Roborock piggyback tree `_async_update_data` itself
    needs (docs/14 rule 1 — no second implementation of this logic for
    tests). `home_frame`/`registration` are `None` and every `*_home_px` is
    `None`/`[]` when this duid has no assignment yet (self-test disabled,
    not polled since restart, or `unaligned` with no matching frame) — the
    card falls back to today's per-vacuum px contract in that case."""
    frame_id = robot_frame.get(device.duid)
    frame = home_frames.get(frame_id) if frame_id else None
    rec = (frame.get("robots") or {}).get(device.duid) if frame is not None else None
    if frame is None or rec is None:
        device.data["home_frame"] = None
        device.data["registration"] = None
        device.data["vacuum_position_home_px"] = None
        device.data["charger_home_px"] = None
        device.data["path_dry_home_px"] = []
        device.data["path_wet_home_px"] = []
        for room in device.data.get("rooms", []):
            room["bbox_home_px"] = None
            room["outline_home_px"] = None
            room["home_room_id"] = None
        return

    cell_mm = frame.get("cell_mm", homeframe.FRAME_CELL_MM)
    scale = frame.get("scale", homeframe.HOME_PX_SCALE)
    device.data["home_frame"] = {
        "id": frame_id,
        "cell_mm": cell_mm,
        "scale": scale,
        "width_px": frame["width"] * scale,
        "height_px": frame["height"] * scale,
    }
    device.data["registration"] = {
        "status": rec.get("status"),
        "method": rec.get("method"),
        "rotation_deg": rec.get("rot_deg"),
        "score": rec.get("score"),
        "iou": rec.get("iou"),
        "wall_conflict": rec.get("wall_conflict"),
    }
    device.data["vacuum_position_home_px"] = _home_px_point(
        device.data.get("vacuum_position"), rec, frame
    )
    device.data["charger_home_px"] = _home_px_point(device.data.get("charger"), rec, frame)
    device.data["path_dry_home_px"] = [
        seg_px
        for seg in path_dry
        if (seg_px := [q for p in seg if (q := _home_px_point(p, rec, frame))])
    ]
    device.data["path_wet_home_px"] = [
        seg_px
        for seg in path_wet
        if (seg_px := [q for p in seg if (q := _home_px_point(p, rec, frame))])
    ]
    # `outline_home_px`/`home_room_id` (docs/40 §4.2 point 6 "vedlejší
    # produkt"): from the in-memory-only per-segment masks cached at the
    # last registration event (never a fresh decode on the hot poll path —
    # see `_home_frame_room_masks`'s own docstring in `__init__`).
    frame_masks = room_masks.get(frame_id) or {}
    seg_masks = frame_masks.get(device.duid) or {}
    home_room_ids = homeframe.room_identity(frame_masks) if frame_masks else {}
    for room in device.data.get("rooms", []):
        p0 = _home_px_point({"x": room.get("x0"), "y": room.get("y0")}, rec, frame)
        p1 = _home_px_point({"x": room.get("x1"), "y": room.get("y1")}, rec, frame)
        if p0 and p1:
            room["bbox_home_px"] = {
                "x0": min(p0["x"], p1["x"]),
                "y0": min(p0["y"], p1["y"]),
                "x1": max(p0["x"], p1["x"]),
                "y1": max(p0["y"], p1["y"]),
            }
        else:
            room["bbox_home_px"] = None
        seg_id = room.get("segment_id")
        mask = seg_masks.get(seg_id) if seg_id is not None else None
        if mask is not None and mask.any():
            outline_local = homeframe.outline_from_mask(
                mask, cell_mm, label=f"{device.name} / {room.get('name')} (segment {seg_id})"
            )
            room["outline_home_px"] = [
                _frame_mm_to_home_px(frame["origin_mm"][0] + ox, frame["origin_mm"][1] + oy, frame)
                for ox, oy in outline_local
            ] or None
        else:
            room["outline_home_px"] = None
        room["home_room_id"] = home_room_ids.get((device.duid, seg_id)) if seg_id is not None else None


def _extract_debug(map_data: Any) -> dict[str, Any]:
    """Debug exposure of so-far unadopted MapData fields (docs/17 §2) — for reverse
    engineering which sources are worth adopting (goto/predicted path for transit
    labeling, no-go areas for coverage totals, …). Everything defensive: a missing
    field just reports 0 / []."""
    out: dict[str, Any] = {}

    # Navigation routes — the transit-labeling candidates.
    for name in ("goto_path", "predicted_path"):
        obj = getattr(map_data, name, None)
        pts: list[dict[str, float]] = []
        subpaths = getattr(obj, "path", None) if obj is not None else None
        if subpaths:
            for sub in subpaths:
                for pt in sub:
                    px = getattr(pt, "x", None)
                    py = getattr(pt, "y", None)
                    if px is not None and py is not None:
                        pts.append({"x": px, "y": py})
        out[f"{name}_points"] = len(pts)
        out[name] = _decimate(pts, 60)

    def _shape_list(attr: str) -> list[dict[str, float]]:
        res: list[dict[str, float]] = []
        for obj in getattr(map_data, attr, None) or []:
            shape: dict[str, float] = {}
            for k in ("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3", "x", "y"):
                v = getattr(obj, k, None)
                if v is not None:
                    shape[k] = v
            if shape:
                res.append(shape)
        return res

    # NOTE: `walls` are USER-DRAWN virtual walls (differ per robot). Physical walls =
    # the image pixel layer ("Show walls" render toggle draws them from pixels).
    out["walls"] = _shape_list("walls")
    out["no_go_areas"] = _shape_list("no_go_areas")
    out["no_mopping_areas"] = _shape_list("no_mopping_areas")
    out["no_carpet_areas"] = _shape_list("no_carpet_areas")
    out["zones"] = _shape_list("zones")
    out["cleaned_areas"] = _shape_list("cleaned_areas") or _shape_list("cleaned_area")
    out["goto"] = _point(getattr(map_data, "goto", None))

    def _obstacle_list(attr: str) -> list[dict[str, Any]]:
        res: list[dict[str, Any]] = []
        for ob in getattr(map_data, attr, None) or []:
            rec: dict[str, Any] = {}
            for k in ("x", "y"):
                v = getattr(ob, k, None)
                if v is not None:
                    rec[k] = v
            for k in ("type", "details"):
                v = getattr(ob, k, None)
                if v is not None:
                    rec[k] = str(v)
            if rec:
                res.append(rec)
        return res

    for attr in ("obstacles", "obstacles_with_photo", "ignored_obstacles", "ignored_obstacles_with_photo"):
        obs = _obstacle_list(attr)
        out[attr] = obs[:30]
        out[f"{attr}_count"] = len(obs)

    # Presence/size probes for heavier structures.
    for name in ("blocks",):
        v = getattr(map_data, name, None)
        try:
            out[f"{name}_count"] = len(v) if v is not None else 0
        except TypeError:
            out[f"{name}_count"] = 1 if v is not None else 0

    # Carpet detection (ultrasonic models, e.g. S7): cells the ROBOT believes are
    # carpet — false positives (mats, thresholds, glossy tiles) are common, so the
    # coordinates matter more than the count.
    carpet = getattr(map_data, "carpet_map", None)
    try:
        out["carpet_map_count"] = len(carpet) if carpet is not None else 0
    except TypeError:
        out["carpet_map_count"] = 1 if carpet is not None else 0
    carpet_sample: list[Any] = []
    if carpet:
        try:
            for item in list(carpet)[:30]:
                p = _point(item)
                carpet_sample.append(p if p is not None else str(item))
        except TypeError:
            carpet_sample = [str(carpet)[:200]]
    out["carpet_map_sample"] = carpet_sample
    img = getattr(map_data, "image", None)
    out["image_present"] = img is not None
    out["image_data_type"] = type(getattr(img, "data", None)).__name__ if img is not None else None

    # docs/40 Fáze 0: raw-map inventory, purely diagnostic (no behaviour depends on
    # these). `map_index`/`map_sequence` come from the parser's own header read,
    # exposed via `additional_parameters` (vacuum-map-parser-roborock 0.1.5) — never
    # assume the key exists, a parser version bump may drop or rename it.
    add_params = getattr(map_data, "additional_parameters", None)
    out["map_index"] = add_params.get("map_index") if isinstance(add_params, dict) else None
    out["map_sequence"] = add_params.get("map_sequence") if isinstance(add_params, dict) else None
    # `image.dimensions` already reflects the library's own trim/crop, which can
    # differ from the raw IMAGE block header docs/40 §2 decodes directly from
    # bytes — recording both here is exactly the discrepancy Fáze 0's report is
    # meant to surface, not something to reconcile in code.
    dims = getattr(img, "dimensions", None) if img is not None else None
    out["image_top"] = getattr(dims, "top", None) if dims is not None else None
    out["image_left"] = getattr(dims, "left", None) if dims is not None else None
    out["image_width"] = getattr(dims, "width", None) if dims is not None else None
    out["image_height"] = getattr(dims, "height", None) if dims is not None else None

    # Field inventory — the definitive ground truth: every public, non-callable field
    # the parser actually carries on THIS model. Ends the guesswork about what exists.
    def _fields(obj: Any) -> list[str]:
        names: list[str] = []
        for k in dir(obj):
            if k.startswith("_"):
                continue
            try:
                v = getattr(obj, k)
            except Exception:  # noqa: BLE001 - a raising property is still a field
                names.append(k + "!")
                continue
            if callable(v):
                continue
            names.append(k)
        return sorted(names)

    out["map_data_fields"] = _fields(map_data)
    out["image_fields"] = _fields(img) if img is not None else []

    return out


def _resolve_map_content(coord: Any) -> Any | None:
    """Resolve the current (or best-effort fallback) ``MapContent`` object for one
    Roborock v1 coordinator. Factored out of `_extract_device` (docs/40 Fáze 0) so
    `AnyVacCoordinator.raw_map_for` — a second, on-demand caller needing the same
    object, for `anyvac.dump_raw_map` — does not reimplement this walk a second
    time (docs/14 rule 1 / docs/34 finding B4). Same defensive getattr-chain,
    same "prefer current map, else any cached one" fallback as before."""
    home = getattr(getattr(coord, "properties_api", None), "home", None)
    if home is None:
        return None
    contents = getattr(home, "home_map_content", None) or {}
    current = getattr(home, "current_map_data", None)
    flag = getattr(current, "map_flag", None)
    map_content = contents.get(flag) if flag is not None else None
    if map_content is None:
        map_content = next(iter(contents.values()), None)
    return map_content


def _dock_error_name(err: Any) -> str | None:
    """Name of a set dock error (`RoborockDockErrorCode`), None for none/ok."""
    try:
        if err is None or int(err) == 0:
            return None
    except (TypeError, ValueError):
        return None
    name = getattr(err, "name", None)
    return str(name) if name else f"code_{int(err)}"


def _raw_map_bytes(coord: Any) -> bytes | None:
    """Raw map bytes of one Roborock v1 coordinator's current map, or None
    (docs/47 — the change test behind the map-snapshot driven refresh)."""
    map_content = _resolve_map_content(coord)
    raw = getattr(map_content, "raw_api_response", None) if map_content else None
    return raw if isinstance(raw, (bytes, bytearray)) else None


def _compute_home_frame_result(
    frames_snapshot: dict[str, dict[str, Any]],
    robot_frame_snapshot: dict[str, str],
    duid: str,
    raw: bytes,
    map_index: Any,
    map_sequence: Any,
    grid_sha1: str,
    lib_rooms: dict[int, Any],
    now_iso: str,
) -> dict[str, Any]:
    """Pure docs/40 §4.2 home-frame registration step for ONE duid's changed
    map. A pure function — everything it needs is passed in as a snapshot
    (never live coordinator state) and everything it returns is a plain
    description of what changed — so `AnyVacCoordinator` can run it in
    `hass.async_add_executor_job` while other work continues on the event
    loop. Frames are only ever REPLACED wholesale (`homeframe.
    merge_robot_into_frame`/`grow_frame_canvas` both copy-on-write), so a
    concurrent caller still holding an old frame object from `frames_snapshot`
    is never corrupted by this call's result being applied afterwards.

    Lifecycle (docs/40 §4.2, numbered to match that section):
    1. Decode + self-test against the library's own room bboxes; a failed
       self-test PERMANENTLY disables registration for this duid (never a
       silent "best effort" geometry) — ``disabled: True``.
    2. No frames exist anywhere yet -> this duid founds the very first frame,
       identity (0°, 0, 0), status ``reference``.
    3. Otherwise: try registering this duid's floor mask against the frame it
       is ALREADY assigned to first (continuity — point 5 below), then, if
       that fails the gate (or it has no assignment yet), against every OTHER
       known frame, keeping the best gate-passing result.
    4. Nothing passes the gate anywhere -> a brand-new frame is founded for
       this duid. If frames already existed elsewhere, this is published as
       ``unaligned`` (docs/40 §4.2 point 4: "different floor" is the expected
       reading, even though the new frame's own internal record for this duid
       is technically ``method: reference``); if `frames_snapshot` was
       completely empty this really is the first robot ever, so ``reference``.
    5. A duid that founded its OWN frame, remapped, and now fails to
       re-register against that SAME frame ("opravdu jiný byt") gets a new
       frame; the old one is kept (never deleted) and flagged
       ``stale_frame_id`` for the caller to mark ``{"stale": True}`` so the
       card can prompt to re-accept a new floorplan rather than silently
       losing it.
    """
    result: dict[str, Any] = {
        "duid": duid,
        "cache_key": (map_index, map_sequence, grid_sha1),
        "disabled": False,
        "error": None,
        "updated_frames": {},
        "removed_robot_from_frames": [],
        "robot_frame": None,
        "stale_frame_id": None,
        "origin_shifted": False,
        "status": None,
        "score": None,
        "iou": None,
        "method": None,
        "rot_deg": None,
        "wall_conflict": None,
        "segment_masks": {},
    }
    try:
        grid = homeframe.decode_grid(raw)
    except Exception as err:  # noqa: BLE001 - a malformed/short raw blob, never fatal
        result["error"] = f"decode_grid: {err}"
        return result

    if lib_rooms and not homeframe.grid_self_test(grid, lib_rooms):
        result["disabled"] = True
        result["status"] = "disabled"
        result["error"] = "grid_self_test failed"
        return result

    def _record(
        rot_deg: float, tx_mm: float, ty_mm: float, score: float, iou: float, method: str, status: str
    ) -> dict[str, Any]:
        return {
            "rot_deg": rot_deg, "tx_mm": tx_mm, "ty_mm": ty_mm, "score": score, "iou": iou,
            "method": method, "status": status, "map_index": map_index, "map_sequence": map_sequence,
            "grid_sha1": grid_sha1, "updated": now_iso,
        }

    def _found_new_frame(status: str) -> dict[str, Any]:
        margin_cells = max(1, round(homeframe.FRAME_MARGIN_MM / homeframe.FRAME_CELL_MM))
        width = grid.width + 2 * margin_cells
        height = grid.height + 2 * margin_cells
        floor_mask = np.zeros((height, width), dtype=bool)
        wall_mask = np.zeros((height, width), dtype=bool)
        floor_mask[margin_cells : margin_cells + grid.height, margin_cells : margin_cells + grid.width] = grid.floor
        wall_mask[margin_cells : margin_cells + grid.height, margin_cells : margin_cells + grid.width] = grid.wall
        origin_mm = (
            (grid.left - margin_cells) * homeframe.CELL_MM,
            (grid.top - margin_cells) * homeframe.CELL_MM,
        )
        return {
            "origin_mm": origin_mm, "cell_mm": float(homeframe.FRAME_CELL_MM),
            "scale": float(homeframe.HOME_PX_SCALE), "width": width, "height": height,
            "epoch": 1, "floor_mask": floor_mask, "wall_mask": wall_mask,
            "robots": {duid: _record(0.0, 0.0, 0.0, 1.0, 1.0, "reference", status)},
        }

    # Continuity first (docs/40 §4.2 point 5): the frame this duid is already
    # in keeps it as long as it still matches AND other robots share it.
    #
    # 1.57.1: a frame this duid holds ALONE does not short-circuit the search.
    # Registrations used to run concurrently against one snapshot, so on a
    # fresh start every robot saw no frames and founded its own; continuity
    # then kept each one in its private frame forever (it always matches
    # itself). Now a robot alone in its frame joins another frame that passes
    # the gate, and its old frame — left empty — is marked stale.
    #
    # The gate is floor overlap (covered / IoU) AND `wall_conflict`: floor
    # blobs alone matched a one-room cellar to the flat at covered 0.72 / IoU
    # 0.67; its walls cut through the flat's open floor (conflict 0.72+), the
    # same flat's robots stay at 0.21-0.41.
    current_frame_id = robot_frame_snapshot.get(duid)
    candidates: list[str] = [current_frame_id] if current_frame_id in frames_snapshot else []
    candidates += [fid for fid in frames_snapshot if fid not in candidates]

    def _others(fid: str) -> bool:
        return any(d != duid for d in (frames_snapshot[fid].get("robots") or {}))

    passing: list[tuple[str, Any, tuple[float, float, float], float]] = []
    for fid in candidates:
        frame = frames_snapshot[fid]
        if frame.get("stale") or not frame.get("width") or not frame.get("height"):
            continue
        if fid != current_frame_id and not _others(fid):
            continue  # an empty frame is no reference
        reg = homeframe.register_with_fallback(grid.floor, frame["floor_mask"])
        if reg.covered < homeframe.DEFAULT_GATE_COVERED or reg.iou < homeframe.DEFAULT_GATE_IOU:
            continue
        affine = homeframe.affine_from_registration(reg, grid, frame["origin_mm"])
        if fid == current_frame_id and not _others(fid):
            conflict = 0.0  # its own map: walls agree by construction
        else:
            conflict = homeframe.wall_conflict(frame, grid, *affine)
        if conflict > homeframe.DEFAULT_GATE_WALL_CONFLICT:
            continue
        passing.append((fid, reg, affine, conflict))
        if fid == current_frame_id and _others(fid):
            break

    best: tuple[str, Any, tuple[float, float, float], float] | None = None
    shared = [p for p in passing if _others(p[0])]
    pool = shared or passing
    for cand in pool:
        if best is None or (cand[1].covered, cand[1].iou) > (best[1].covered, best[1].iou):
            best = cand
    if shared and current_frame_id in [p[0] for p in shared]:
        best = next(p for p in shared if p[0] == current_frame_id)

    if best is not None:
        fid, reg, (rot_deg, tx_mm, ty_mm), conflict = best
        frame = frames_snapshot[fid]
        merged = homeframe.merge_robot_into_frame(frame, grid, rot_deg, tx_mm, ty_mm)
        was_founder = (frame.get("robots") or {}).get(duid, {}).get("method") == "reference"
        status = "reference" if was_founder else "aligned"
        merged["robots"][duid] = _record(
            rot_deg, tx_mm, ty_mm, reg.covered, reg.iou,
            "reference" if was_founder else reg.method, status,
        )
        merged["robots"][duid]["wall_conflict"] = round(conflict, 3)
        result["updated_frames"][fid] = merged
        result["robot_frame"] = fid
        result["status"] = status
        result["score"] = reg.covered
        result["iou"] = reg.iou
        result["method"] = reg.method
        result["rot_deg"] = rot_deg
        result["wall_conflict"] = conflict
        # docs/40 §4.2 point 6 byproduct (`outline_home_px`/`home_room_id`):
        # one full-frame-sized mask per room segment, registered the SAME
        # point-based way the floor/wall masks were just merged — computed
        # against `merged`'s FINAL shape/origin (post growth/shift), never
        # persisted (docs/40 §4.2's on-disk schema stays exactly as
        # specified — this is coordinator-side, in-memory-only cache).
        result["segment_masks"] = homeframe.segment_masks_in_frame(
            grid, rot_deg, tx_mm, ty_mm, merged["origin_mm"], merged.get("cell_mm", homeframe.FRAME_CELL_MM),
            merged["width"], merged["height"],
        )
        if merged.get("epoch") != frame.get("epoch", 1):
            result["origin_shifted"] = True
        if current_frame_id and current_frame_id != fid:
            result["removed_robot_from_frames"].append(current_frame_id)
        return result

    # Nothing matched anywhere -> found a brand-new frame for this duid
    # (docs/40 §4.2 point 4: "unaligned -> robot dostane vlastní frame").
    new_frame_id = str(uuid.uuid4())
    new_status = "reference" if not frames_snapshot else "unaligned"
    new_frame = _found_new_frame(new_status)
    result["updated_frames"][new_frame_id] = new_frame
    result["robot_frame"] = new_frame_id
    result["status"] = new_status
    result["score"] = 1.0
    result["iou"] = 1.0
    result["method"] = "reference"
    result["rot_deg"] = 0.0
    result["segment_masks"] = homeframe.segment_masks_in_frame(
        grid, 0.0, 0.0, 0.0, new_frame["origin_mm"], new_frame["cell_mm"],
        new_frame["width"], new_frame["height"],
    )
    if current_frame_id and current_frame_id in frames_snapshot:
        was_founder = (frames_snapshot[current_frame_id].get("robots") or {}).get(duid, {}).get(
            "method"
        ) == "reference"
        if was_founder:
            result["stale_frame_id"] = current_frame_id
        else:
            result["removed_robot_from_frames"].append(current_frame_id)
    return result


def _extract_map(map_data: Any, decimate: bool = True) -> dict[str, Any]:
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
    # `decimate=False` (D4, 1.52.3): the decimated `path` / `mop_path` only feed the
    # opt-in legacy mm attributes; with those off they were computed (20-40 ms per
    # vacuum and poll, on the event loop) and then dropped. The raw list is handed
    # over untouched — `_async_update_data` pops it, nothing else reads it.
    out["path"] = _decimate(flat, PATH_MAX_POINTS) if decimate else flat
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
    out["mop_path"] = _decimate(mflat, PATH_MAX_POINTS) if decimate else mflat
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
    # Same {segment_number: Room} dict, kept transiently (docs/40 §4.2/§4.3)
    # so `_maybe_register_home_frame` can pass it straight to
    # `homeframe.grid_self_test` as `lib_rooms` without a second parse of the
    # raw map (docs/14 rule 1) — popped before the data is exposed on the
    # sensor, same as `_path_dry`/`_path_wet` below.
    out["_lib_rooms"] = dict(room_dict)

    # Full (undecimated) trajectories, kept transiently so the coordinator can attribute
    # only the newly-added points to the room currently being cleaned. Dry (vacuum) and
    # wet (mop) are kept separate so coverage can be reported per clean type. Popped
    # before the data is exposed on the sensor.
    out["_path_dry"] = flat
    out["_path_wet"] = mflat

    # Which segments the robot has cleaned (this session) + where it currently is.
    out["cleaned_rooms"] = sorted(getattr(map_data, "cleaned_rooms", None) or [])
    # Segments of the RUNNING clean (docs/45 §2.2): the map's BLOCKS block —
    # one byte per segment id, present only while a segment clean runs, and
    # carrying the whole ordered list (verified on S6/S7/S8 dumps 2026-09-30:
    # a room the robot only drove through is absent, a later target present).
    # `vacuum-map-parser-roborock` keeps it as raw bytes and never fills
    # `cleaned_rooms` from it, which is why `cleaned_rooms` above is empty.
    blocks = getattr(map_data, "blocks", None)
    out["target_segments"] = (
        sorted({int(b) for b in blocks if int(b) > 0})
        if isinstance(blocks, (bytes, bytearray, list, tuple))
        else []
    )
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
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        # 1.51.8 (plan po auditu, B2): every Store's latest delayed-save data
        # function, so `async_shutdown` can write what is still pending. HA's own
        # final write only runs when HA stops; on an entry reload the old
        # coordinator's timers used to fire AFTER the new one had loaded the
        # files, overwriting newer data with the old snapshot — or the pending
        # write was simply lost.
        self._pending_saves: dict[Store, Callable[[], Any]] = {}
        self._store: Store = Store(hass, 1, f"{DOMAIN}_room_history")
        # Read once at construction: `__init__.py` reloads the entry on an
        # options change (`async_update_listener`), so a new coordinator picks
        # up the new value — no need to re-read it on every poll.
        self._expose_legacy_mm: bool = entry.options.get(
            OPT_EXPOSE_LEGACY_MM, DEFAULT_EXPOSE_LEGACY_MM
        )
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
        # First candidate of the two-poll stability check of that capture (docs/49).
        self._signal_candidate: dict[str, str] = {}
        # Verdict of the LAST finished run per vacuum: {kind, source, finished_at,
        # rooms}. Persisted (`_last_run_store`) — the idle `clean_type` is null, so
        # this is what a card/automation reads after the run (docs/49).
        self._last_run_store: Store = Store(hass, 1, f"{DOMAIN}_last_run")
        self._last_run: dict[str, dict[str, Any]] = {}
        # Diagnostics: the last single-room calibration decision per vacuum (for the
        # card's Debug tab) — so you can see exactly why an estimate did / didn't write.
        self._last_calib: dict[str, dict[str, Any]] = {}
        # Card-level room selection (which rooms are queued to clean), shared across
        # devices and fed to the orchestrator. Persisted across restarts.
        self._sel_store: Store = Store(hass, 1, f"{DOMAIN}_selection")
        self._selected_rooms: set[str] = set()
        # Per-room, per-pass vacuum pins (docs/18 §7e, per-kind since 2026-07-25):
        # the user's "clean THIS room's dry/wet pass with THAT robot" overrides,
        # {room_name: {"dry": vacuum entity_id or duid, "wet": ...}}. dry and wet
        # are independent — a fleet with no dual-capable robot (e.g. dry-only S6/S7
        # + wet-only S8) needs to be able to hold both preferences for the same
        # room at once, which a single flat {room: vacuum} value could not do: the
        # last pin set always silently overwrote the other pass's, and the planner
        # would fall back to automatic assignment (with a WARNING) for whichever
        # pass no longer matched the stored vacuum every single time. Shared across
        # devices like the selection; the planner uses this as the default `pin`
        # (an explicit `pin` parameter on anyvac.clean wins). Each pass auto-clears
        # once that pass of the room actually finishes. Persisted across restarts.
        self._pins_store: Store = Store(hass, 1, f"{DOMAIN}_room_pins")
        self._room_pins: dict[str, dict[str, str]] = {}
        # Room cleaning sequence (docs/19): the order configured in the Roborock app,
        # which is dominant regardless of what order HA sends segment ids in — the
        # firmware always visits rooms in this order (confirmed in the field across
        # thousands of cleans). We don't control it, we just need to KNOW it so the
        # planner can predict per-room dry-finish / wet-start timing instead of
        # guessing. {room_name: 1-based position}. Backend-owned (not card config)
        # for the same reason as room_pins: it's a physical fact, not a per-dashboard
        # preference, and multiple cards/browsers would otherwise duplicate it.
        # Persisted across restarts.
        # Legacy store of the hand-edited room order (docs/19, removed in 1.60.0,
        # docs/52 O1). Never read again; kept so uninstall removes the file.
        self._seq_store: Store = Store(hass, 1, f"{DOMAIN}_room_sequence")
        # The running room-by-room job (docs/52 §9.5), saved on every change of an
        # item so a Home Assistant restart resumes it. Written by the job runner.
        self._job_store: Store = Store(hass, 1, f"{DOMAIN}_job")
        self._job_saved: dict[str, Any] | None = None
        # Named service points (docs/52 §5 item 7): {duid: [{name, x, y}]} in
        # the robot's OWN mm — what `app_goto_target` takes, so going there
        # needs no conversion. Persisted across restarts.
        self._sp_store: Store = Store(hass, 1, f"{DOMAIN}_service_points")
        self._service_points: dict[str, list[dict[str, Any]]] = {}
        # The room order set in the Roborock app, read from the robot
        # (`get_clean_sequence`, docs/52 O1): {duid: {room: position}} + when.
        # Only a batch dispatch (or a multi-room clean started outside AnyVac)
        # follows it — room-by-room jobs choose their own order.
        self.app_sequence: dict[str, dict[str, int]] = {}
        self._app_sequence_at: dict[str, datetime] = {}
        # Per-TASK state (docs/52 §9.3): one task = one room in a room-by-room
        # job. `_task_open` = a task is running; `_task_item` = the room(s) the
        # job runner sent with it (the measurement targets); `_task_last` = the
        # last closed task's event, re-fired as `run_finished` once a robot with
        # no job docks.
        self._task_open: dict[str, bool] = {}
        self._task_item: dict[str, set[str]] = {}
        self._task_last: dict[str, dict[str, Any]] = {}
        self._prev_state: dict[str, str | None] = {}
        self._task_rooms_done: dict[str, set[str]] = {}
        # Tasks closed but not learned yet: {duid: {"at": closed, "polls": n}}.
        self._harvest_pending: dict[str, dict[str, Any]] = {}
        # Live per-room elapsed cleaning seconds this session (for the debug progress
        # gauge's time-ratio) + last poll timestamp to measure the per-poll delta.
        self._room_elapsed: dict[str, dict[str, float]] = {}
        self._last_poll: dict[str, datetime] = {}
        # Map-snapshot driven refresh (docs/47). `_rb_listeners`: one listener per
        # Roborock v1 coordinator object we are subscribed to, keyed by id() — the
        # coordinator itself is kept in the tuple so the id cannot be reused while
        # we hold it. `_rb_seen_raw`: the raw map bytes we last PROCESSED per duid
        # (the listener's change test and the poll's freshness test both compare
        # against it). `_map_fresh`: per duid, did THIS poll see a new snapshot.
        self._rb_listeners: dict[int, tuple[Any, CALLBACK_TYPE]] = {}
        # docs/48: live extension of the last full-map snapshot per duid, from
        # the robot's local get_dynamic_map_diff. Display only — nothing in the
        # pipeline reads it (docs/48 §1.1).
        self._live_enabled: bool = entry.options.get(OPT_LIVE_DIFF, DEFAULT_LIVE_DIFF)
        self._live: dict[str, livediff.LiveTrail] = {}
        self._live_pub: dict[str, dict[str, Any]] = {}
        self._live_inflight: set[str] = set()
        self._live_fails: dict[str, int] = {}
        self._live_backoff: dict[str, float] = {}
        # Per-robot poller counters, published as `live_stats` (docs/48 follow-up:
        # field report "still every 30 s" — live updates only right after a
        # full-map refresh; these say what the diff answers in between).
        self._live_stats: dict[str, dict[str, Any]] = {}
        # 1.51.6 (plan po auditu, A0): `clean_type_trace` — the full polls around
        # a clean's falling edge plus the clean_type AnyVac's own clean events
        # carried. Field report 2026-11-12: a dry S7 MaxV run is announced as wet.
        # The fix depends on WHICH path produces it (no room name during the run,
        # or in_cleaning still set while the water mode is already reset), and the
        # sensor's attributes are not recorded, so only a live capture can tell.
        # Observation only: nothing here feeds back into the pipeline.
        self._ct_trace_buf: dict[str, deque[dict[str, Any]]] = {}
        self._ct_trace_prev: dict[str, bool] = {}
        self._ct_trace_tail: dict[str, int] = {}
        self._ct_trace_last: dict[str, dict[str, Any]] = {}
        self._ct_trace_events: dict[str, deque[dict[str, Any]]] = {}
        self._ct_trace_unsub: list[CALLBACK_TYPE] = [
            hass.bus.async_listen(f"{DOMAIN}_{name}", self._ct_trace_on_event)
            for name in ("clean_started", "clean_finished", "run_finished")
        ]
        # Robots whose firmware rejects get_dynamic_map_diff ("method not
        # recognized" — S6, field-caught 2026-10-08). Never polled again until
        # HA restarts: retrying every 60 s would only produce the same error.
        self._live_unsupported: set[str] = set()
        self._live_nodiff: dict[str, int] = {}  # consecutive diff-less answers
        self._rb_seen_raw: dict[str, bytes] = {}
        self._map_fresh: dict[str, bool] = {}
        # Plan-scope transit labeling (docs/17 §1.3): room-name scope of the currently
        # running orchestrated job (anyvac.clean), PER VACUUM — set by services.py's
        # _JobRunner when a job starts, cleared when it finishes/cancels. Not persisted
        # (a job-in-progress does not survive a restart anyway). None/absent = no active
        # job scope known for that duid (manual/native/degraded runs) — attribution then
        # falls back to today's state-only TRANSIT_STATES gating, unchanged.
        self._job_rooms: dict[str, set[str]] = {}
        # Monotonic id of the currently active job per vacuum, and the id the
        # STORED path currently belongs to (docs/27 follow-up, 2026-07-26). A
        # `job_rooms` presence check alone cannot tell "still the same job's
        # next sortie" apart from "a brand new job's first sortie" — both look
        # identical (job_rooms just became truthy again) the moment a new
        # `anyvac.clean` starts, which is exactly the confirmed field bug: the
        # cross-sortie stitching from docs/27 accidentally also stitched
        # across ENTIRELY SEPARATE job runs, so a vacuum's trace never got
        # wiped again once it had run inside any orchestrated job at least
        # once. `_job_id` increments on every `set_job_rooms(duid, rooms)`
        # start call; comparing it against `_path_job_id` (see
        # `_sortie_is_new_job`) is what actually distinguishes the two cases.
        self._job_seq = 0
        self._job_id: dict[str, int] = {}
        # Live progress of the running orchestrated job (docs/44 F2): the plan's
        # room passes in order, with per-pass done timestamps. Set/cleared by
        # services.py's _JobRunner; not persisted — a job does not survive a
        # restart, and neither should a stale "done around HH:MM".
        self._job_state: dict[str, Any] | None = None
        self._path_job_id: dict[str, int | None] = {}
        # Room completion (docs/45): one `coverage.RunTracker` per vacuum for the
        # whole RUN (docs/36 — a dock trip between two batches of one job keeps it).
        # It decides which room is really being cleaned (run targets + sequence +
        # dwell, so rooms merely driven through never get a number) and holds the
        # footprint cells + path length per room and kind. Not persisted — an
        # in-flight run does not survive a restart, same as `_job_rooms`.
        self._runs: dict[str, covmod.RunTracker] = {}
        # Rooms named as targets so far in the current run (map BLOCKS of every
        # sortie ∪ the job scope) — a later batch's BLOCKS never forgets an
        # earlier one's rooms. {duid: set[room_name]}.
        self._run_targets_seen: dict[str, set[str]] = {}
        # Was the previous poll genuinely cleaning (in_cleaning and not transit)?
        # The poll right after it closes is still attributed, restricted to the
        # active room (docs/45 §1.2). {duid: bool}.
        self._cov_gate: dict[str, bool] = {}
        # Coverage geometry per vacuum, rebuilt only when the map image or room
        # list changes: {duid: (fast_key, geometry_key, coverage.Geometry)}.
        self._geo: dict[str, tuple[Any, Any, covmod.Geometry]] = {}
        # How many dry/wet trajectory points have already been attributed, per vacuum, so
        # each poll only processes the newly-added points. {duid: {"dry": n, "wet": n}}.
        self._path_seen: dict[str, dict[str, int]] = {}
        # Legacy store of the pre-docs/45 learned coverage baselines — no longer
        # read; removed from disk once at setup (`_async_setup`).
        self._cov_store: Store = Store(hass, 1, f"{DOMAIN}_coverage_baseline")
        # Persistent per-room completion of the LAST run that cleaned the room
        # (docs/29 → docs/45): keyed like `_history` (room NAME, shared across the
        # fleet; whichever pass most recently finished wins). Values: "dry"/"wet" =
        # % of the ordered work done (passes included, 100 for a room the robot
        # finished), "dry_floor"/"wet_floor" = % of the reachable floor the footprint
        # covered — the card's "part of the room was not reachable" warning.
        # Missing kind = never measured; the card shows "—".
        self._cov_pct_store: Store = Store(hass, 1, f"{DOMAIN}_room_completion")
        self._cov_legacy_pct_store: Store = Store(hass, 1, f"{DOMAIN}_room_coverage_pct")
        self._room_coverage: dict[str, dict[str, int]] = {}
        # Reach mask (docs/51 §2): {duid: {room: record}} — the swept footprint of
        # the robot's last complete clean of each room (`reach.make_record`),
        # replaced by the next one. Rendered onto the robot's own map canvas and
        # served by the `image` platform (authenticated, never a /local/ file:
        # it is the floor plan of the user's home, same reasoning as 1.51.9 C2).
        self._reach_store: Store = Store(hass, 1, f"{DOMAIN}_reach_masks")
        self._reach_masks: dict[str, dict[str, dict[str, Any]]] = {}
        self._reach_rev: dict[str, int] = {}
        self._reach_png: dict[str, bytes | None] = {}
        # Same records on the robot's home-frame canvas (docs/51 §2.5, home variant).
        self._reach_png_home: dict[str, bytes | None] = {}
        self._reach_updated: dict[str, datetime | None] = {}
        self._reach_sig: dict[str, tuple[Any, ...]] = {}
        self._reach_busy: set[str] = set()
        # Segmented DRY trace (docs/14 §3.9): the parser's ``path`` is the robot's FULL
        # trajectory (transit, mop-wash trips and goto included), so it must not be shown
        # as the "dry" layer directly. Only points recorded while actively cleaning and
        # not in a TRANSIT state are appended here.
        # Stored as a LIST OF SEGMENTS (not one flat list): every time the gate closes
        # (transit / mop-wash / not-vacuuming) and reopens, a NEW segment starts. A flat
        # list would splice the last kept point before a gap directly to the first kept
        # point after it, and the card's single <polyline> would draw a straight line
        # across the gap — visible as spurious diagonal streaks once a session has
        # covered more than one room (bug found 2026-07-15: finished traces looked like
        # scribbles while the live in-progress trace still looked clean).
        # {duid: [ [ {x, y}, ... ], [ {x, y}, ... ], ... ]}.
        # Persisted across restarts since 2026-07-26 (see `_paths_store` below) —
        # this is the user-visible "last clean" trace, not just live-session
        # scratch state; a HA restart must not blank it or replace it with an
        # unsegmented dump of the robot's own raw buffer (field report: dry
        # showed nothing at all post-restart since its gate never reopens for
        # an idle robot, wet showed one giant undifferentiated blob since it has
        # no such gate — see `_paths_store`/`_paths_for_save` for the fix).
        self._dry_path: dict[str, list[list[dict[str, float]]]] = {}
        # Whether the gate was open on the immediately preceding poll, per vacuum — tells
        # `_attribute_points` whether new points continue the last segment or must start
        # a fresh one. Not the same as "new points arrived": a poll with the gate open but
        # zero new points (robot paused, suction still on) must NOT start a new segment.
        # Persisted alongside `_dry_path` (same reason).
        self._dry_path_open: dict[str, bool] = {}
        # Wet (mop) trace, segmented the same shape as `_dry_path` (docs/27) — needed so
        # cross-sortie stitching (below) can bridge a dock trip with a new segment instead
        # of a straight line, same as dry. Unlike dry, wet needs no semantic gate: the
        # parser's `mop_path` already only contains points while the mop is physically
        # down, so a poll with new wet points always continues (or opens) the current
        # segment; `_wet_path_open` is only forced closed at a genuine job/session
        # boundary (never mid-sortie), so a slow mop pass never gets fragmented.
        # Persisted alongside `_dry_path` (same reason).
        self._wet_path: dict[str, list[list[dict[str, float]]]] = {}
        self._wet_path_open: dict[str, bool] = {}
        # Persistence for the four dicts above (2026-07-26 follow-up to docs/27):
        # without this, a HA restart wiped the segmented trace entirely — the
        # very next poll then re-diffed the robot's raw path buffers against a
        # freshly-empty `_path_seen`, which (a) for wet unconditionally dumped
        # the robot's ENTIRE last-session raw mop path as one undifferentiated
        # blob (no semantic gate to stop it) and (b) for dry produced nothing at
        # all unless the robot happened to be actively vacuuming at that exact
        # poll (its gate requires live state). Net effect: "I only see some old
        # mop path, no dry trace" after every restart — reported 2026-07-26.
        # The fix persists our OWN already-gated/segmented trace + the bookkeeping
        # needed to resume diffing correctly (`_path_seen`/open-flags), so a
        # restart shows exactly the last clean as it looked before restart, and
        # only a genuinely NEW job/sortie (`_sortie_is_new_job`) wipes it — never
        # the restart itself.
        self._paths_store: Store = Store(hass, 1, f"{DOMAIN}_paths")
        # Pipeline observability (docs/13 B6): warn (once) when a previously seen vacuum
        # stops yielding map data — the roborock internals may have changed.
        self._known_duids: set[str] = set()
        self._pipeline_warned = False
        # Shared dry/wet layer visibility (card view state, synced across devices the
        # same way as the room selection). Persisted across restarts.
        self._layers_store: Store = Store(hass, 1, f"{DOMAIN}_view_layers")
        self._view_layers: dict[str, bool] = {"dry": True, "wet": False}
        # Visual editor override layer (docs/41 §4.6 + docs/42 appearance +
        # rooms extensions), keyed by floorplan identity (`image_base.src`,
        # exactly as the config has it — no normalization): {src: {"vacuums":
        # {entity: {"map": {rotation, scale, scale_y?, offset_x, offset_y},
        # "appearance": {...}, "rooms": {room_key: {map_x?, map_y?, map_w?,
        # map_h?, area_id?}}}}, "image_base": dict|None, "rooms": {room_key:
        # {...}} (card-level, merged mode — sibling of "image_base", NOT
        # nested under "vacuums"), "updated": iso}}. "map", "appearance" and
        # "rooms" are each independently optional per vacuum entry — any
        # subset present is normal (a vacuum with only an appearance override
        # and no seat override, or vice versa, etc.). `rooms` (both the
        # per-vacuum and the card-level one) further merges at the room_key
        # level rather than being a single atomic value (docs/42 §4.4, fáze K
        # — see `set_floorplan_seat`'s own docstring for why, and for how it
        # relates to `image_base`'s OWN atomic clear-on-omit contract when
        # both live in the same card-level branch). `image_base`'s own shape
        # (crop_box/home_anchors) is phase G — stored/returned opaquely,
        # never interpreted here. Shared across devices/dashboards like
        # room_pins/view_layers above, for the same reason: one alignment
        # should apply to every card showing that floorplan, not be
        # duplicated per-dashboard. Persisted across restarts. BREAKING
        # (integration 1.12.0): the per-vacuum shape changed from a flat
        # `{rotation,...}` dict to this nested `{"map": ..., "appearance":
        # ...}` shape — no migration, any previously stored data is discarded
        # at load time (docs/42). `rooms` (1.13.0, fáze K) is purely additive
        # on top of that 1.12.0 shape — not another breaking change.
        self._seats_store: Store = Store(hass, 1, f"{DOMAIN}_floorplan_seats")
        self._floorplan_seats: dict[str, dict[str, Any]] = {}
        # Debug watermarks (docs/17 §2): goto/predicted paths are TRANSIENT — they only
        # exist while the robot navigates and are gone by the time anyone reads the
        # sensor manually. Remember the last non-empty sighting per vacuum so a regular
        # clean answers "does the firmware publish them?" without manual timing.
        self._debug_seen: dict[str, dict[str, Any]] = {}
        # Per-duid, per-layer memo for `_decimate_segments` (1.1.0) — see its
        # docstring. {duid: {"dry"/"wet": {segment_index: ((len, budget), pts)}}}.
        # Dropped wholesale whenever a trace is wiped, so it can never outlive
        # the segments it describes.
        self._decim_cache: dict[str, dict[str, dict[int, Any]]] = {}
        # Home frame (docs/40 §4.1-4.2, Fáze 1): a shared raster + per-robot
        # rigid registration onto it, built purely from raw map geometry —
        # no card/config involvement (additive contract v3, see sensor.py).
        # In memory, `_home_frames[frame_id]["floor_mask"/"wall_mask"]` are
        # numpy bool arrays (never the base64 `Store` encoding — that only
        # exists on disk, via `homeframe.frames_to/from_storage`).
        # {frame_id: {"origin_mm", "cell_mm", "scale", "width", "height",
        #             "epoch", "floor_mask", "wall_mask", "robots": {duid: {...}}}}.
        # Persisted across restarts (debounced, like `_paths_store` — never on
        # every 30s poll unless a registration actually changed something).
        self._home_frame_store: Store = Store(hass, 1, f"{DOMAIN}_home_frame")
        self._home_frames: dict[str, dict[str, Any]] = {}
        # Which frame each robot currently belongs to. {duid: frame_id}.
        self._robot_frame: dict[str, str] = {}
        # Cache key (docs/40 §4.3) so an unchanged map never re-triggers
        # registration on a plain 30s poll: {duid: (map_index, map_sequence,
        # grid_sha1)}. Populated/consulted by the Fáze 1 registration pipeline.
        self._home_frame_cache_key: dict[str, tuple[Any, Any, str]] = {}
        # Grid self-test (docs/40 §4.3): once a duid's decoded grid fails the
        # self-test against the library's own room bboxes, home-frame
        # registration is disabled for it (no `*_home_px`, rest of the
        # pipeline unaffected) and the WARNING is logged only once.
        self._home_frame_disabled: set[str] = set()
        # Per-duid busy-set (docs/40 §4.3 "jen v executoru") — NOT a real
        # asyncio.Lock, just a set checked/added/discarded on the event loop
        # thread only, so it never races: prevents dispatching a second
        # executor job for a duid whose first one hasn't finished yet (a slow
        # ~5s registration overlapping the next 30s poll).
        self._home_frame_inflight: set[str] = set()
        # docs/40 §4.2 point 6 byproduct (`outline_home_px`/`home_room_id`):
        # one full-frame-sized bool mask per room segment per duid, IN
        # MEMORY ONLY (never persisted — `homeframe.frame_to_storage` doesn't
        # know this key, so it simply doesn't survive a restart; the next
        # registration event for that duid rebuilds it, no correctness
        # issue, just a brief gap right after startup). Rebuilt wholesale
        # per duid on every registration event (`_async_run_home_frame_
        # registration`), never mutated in place. {frame_id: {duid: {segment_id: mask}}}.
        self._home_frame_room_masks: dict[str, dict[str, dict[int, Any]]] = {}

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

    # ── Reach mask (docs/51 §2) ─────────────────────────────────────────────

    @staticmethod
    def _map_index(device: AnyVacDevice) -> Any:
        return (device.data.get("debug_map") or {}).get("map_index")

    def _reach_ready(self) -> bool:
        """Unit tests build partial coordinators (`object.__new__`) for the poll
        pipeline; the reach mask is an add-on there, never a reason to crash."""
        if "_reach_masks" in self.__dict__:
            return True
        self._reach_masks, self._reach_rev, self._reach_png = {}, {}, {}
        self._reach_png_home = {}
        self._reach_updated, self._reach_sig, self._reach_busy = {}, {}, set()
        self._reach_store = None  # type: ignore[assignment]
        return False

    def _reach_save(self, delay: float) -> None:
        if self._reach_store is not None:
            self._delay_save(self._reach_store, lambda: self._reach_masks, delay)

    def _record_reach(self, device: AnyVacDevice, run: covmod.RunTracker, geo: Any) -> bool:
        """After a finished run: replace the reach record of every room the run
        really cleaned and swept enough of (`reach.make_record`). Rooms that do
        not qualify keep whatever was stored."""
        self._reach_ready()
        if geo is None:
            return False
        duid = device.duid
        at = dt_util.utcnow().isoformat(timespec="seconds")
        map_index = self._map_index(device)
        changed = False
        for nm in run.activated:
            rg = geo.rooms.get(nm)
            kinds = run.rooms.get(nm) or {}
            if rg is None or not kinds:
                continue
            cells: set[int] = set()
            used: list[str] = []
            for k, kr in kinds.items():
                if kr.cells:
                    cells |= kr.cells
                    used.append(k)
            rec = reachmod.make_record(
                cells, rg.reach, segment_id=rg.segment_id, kinds=used, map_index=map_index, at=at,
            )
            if rec is None:
                continue
            self._reach_masks.setdefault(duid, {})[nm] = rec
            changed = True
        if changed:
            self._reach_rev[duid] = self._reach_rev.get(duid, 0) + 1
            self._reach_save(5)
        return changed

    def reset_reach(self, duid: str | None = None, room: str | None = None) -> None:
        """anyvac.reset_reach — forget stored reach records (one robot, one room,
        or everything). The next complete clean of a room records it again."""
        self._reach_ready()
        for dd in list(self._reach_masks):
            if duid and dd != duid:
                continue
            rooms = self._reach_masks[dd]
            for nm in list(rooms):
                if room and nm != room:
                    continue
                rooms.pop(nm)
            if not rooms:
                self._reach_masks.pop(dd)
            self._reach_rev[dd] = self._reach_rev.get(dd, 0) + 1
        self._reach_save(2)
        self.async_update_listeners()

    def _reach_current(self, device: AnyVacDevice) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """(records that still match the robot's current map, per-room meta incl.
        stale ones) for this robot."""
        self._reach_ready()
        rooms_now = {
            r.get("name"): r for r in device.data.get("rooms") or [] if r.get("name")
        }
        live: list[dict[str, Any]] = []
        meta: dict[str, dict[str, Any]] = {}
        for nm, rec in sorted((self._reach_masks.get(device.duid) or {}).items()):
            room = rooms_now.get(nm) or {}
            stale = reachmod.is_stale(rec, room.get("segment_id"), room)
            meta[nm] = {
                "at": rec.get("at"),
                "floor_pct": rec.get("floor_pct"),
                "kinds": rec.get("kinds"),
                "stale": stale,
            }
            if not stale:
                live.append(rec)
        return live, meta

    def _reach_canvas(self, device: AnyVacDevice) -> tuple[tuple[float, ...], int, int] | None:
        aff = _solve_affine(device.data.get("calibration_points"))
        dims = device.data.get("image_dims") or {}
        width, height = dims.get("width"), dims.get("height")
        scale = dims.get("scale") or 1
        if aff is None or not width or not height:
            return None
        return aff, int(round(width * scale)), int(round(height * scale))

    def reach_render_args(
        self, duid: str, device: AnyVacDevice | None = None
    ) -> tuple[int, int, Any, tuple[float, ...], int, int] | None:
        """`(x0, y0, mask, aff, width, height)` for `reach.render_rgba` — the
        union of this robot's current (non-stale) records on its own map canvas.
        None when there is nothing to draw or no calibration yet.

        `device` is the poll's fresh device; without it the last published one
        (`self.data`) is used — which is None during the first poll after a
        restart (1.57.1)."""
        device = device or (self.data or {}).get(duid)
        if device is None:
            return None
        live, _meta = self._reach_current(device)
        canvas = self._reach_canvas(device)
        union = reachmod.union_mask(live)
        if canvas is None or union is None:
            return None
        aff, w, h = canvas
        x0, y0, mask = union
        return x0, y0, mask, aff, w, h

    def _home_affine(self, duid: str) -> tuple[float, float, float, float, float, float] | None:
        """This robot's mm -> home-frame px as one affine, probed through the SAME
        `mm_to_home_px` every `*_home_px` attribute uses (registration rotation +
        translation, then the frame's origin/cell/scale — all affine, so three
        points determine it exactly; docs/14 rule 1, no second transform)."""
        p0 = self.mm_to_home_px(duid, 0.0, 0.0)
        px = self.mm_to_home_px(duid, 1000.0, 0.0)
        py = self.mm_to_home_px(duid, 0.0, 1000.0)
        if p0 is None or px is None or py is None:
            return None
        return (
            (px[0] - p0[0]) / 1000.0, (py[0] - p0[0]) / 1000.0, p0[0],
            (px[1] - p0[1]) / 1000.0, (py[1] - p0[1]) / 1000.0, p0[1],
        )

    def _home_canvas(self, duid: str) -> tuple[str, tuple[float, ...], int, int] | None:
        frame_id = self._robot_frame.get(duid) if "_robot_frame" in self.__dict__ else None
        frame = self._home_frames.get(frame_id) if frame_id else None
        aff = self._home_affine(duid) if frame is not None else None
        if frame is None or aff is None:
            return None
        scale = frame.get("scale", homeframe.HOME_PX_SCALE)
        return frame_id, aff, int(frame["width"] * scale), int(frame["height"] * scale)

    def reach_home_args(
        self, duid: str, device: AnyVacDevice | None = None
    ) -> tuple[int, int, Any, tuple[float, ...], int, int] | None:
        """Like `reach_render_args`, on the robot's home-frame canvas — the frame's
        own px space (`*_home_px`, `crop_box.frame_id`). None without a
        registration or without current records."""
        device = device or (self.data or {}).get(duid)
        if device is None:
            return None
        live, _meta = self._reach_current(device)
        canvas = self._home_canvas(duid)
        union = reachmod.union_mask(live)
        if canvas is None or union is None:
            return None
        _fid, aff, w, h = canvas
        x0, y0, mask = union
        return x0, y0, mask, aff, w, h

    def _reach_publish(self, device: AnyVacDevice) -> None:
        """Per poll: publish the per-room meta, and re-render the rasters (robot
        map canvas + home-frame canvas) when their inputs changed (records,
        staleness, calibration, registration or canvas size)."""
        self._reach_ready()
        duid = device.duid
        live, meta = self._reach_current(device)
        canvas = self._reach_canvas(device)
        home = self._home_canvas(duid)
        sig = (
            self._reach_rev.get(duid, 0),
            tuple(sorted(r.get("at") or "" for r in live)),
            tuple(round(v, 4) for v in canvas[0]) if canvas else None,
            canvas[1:] if canvas else None,
            (home[0], tuple(round(v, 4) for v in home[1]), home[2], home[3]) if home else None,
        )
        device.data["reach"] = {
            "entity_id": self._reach_entity_id(duid),
            "home_entity_id": self._reach_entity_id(duid, home=True),
            "home_frame_id": home[0] if home else None,
            "updated": (u.isoformat(timespec="seconds") if (u := self._reach_updated.get(duid)) else None),
            "home_ready": self._reach_png_home.get(duid) is not None,
            "rooms": meta,
        }
        if self._reach_sig.get(duid) == sig or duid in self._reach_busy:
            return
        self._reach_sig[duid] = sig
        # 1.57.1: render from THIS poll's device. `self.data` is still None
        # during the first poll after a restart; reading it there produced no
        # args, the signature was stored anyway, and nothing re-rendered until
        # the next recorded clean — every restart blanked the reach images.
        args = self.reach_render_args(duid, device)
        hargs = self.reach_home_args(duid, device)
        if args is None and hargs is None:
            if self._reach_png.get(duid) is not None or self._reach_png_home.get(duid) is not None:
                self._reach_png[duid] = None
                self._reach_png_home[duid] = None
                self._reach_updated[duid] = dt_util.utcnow()
            return
        self._reach_busy.add(duid)

        def _one(a: Any) -> bytes | None:
            if a is None:
                return None
            rgba = reachmod.render_rgba(*a)
            return reachmod.png_bytes(rgba) if rgba is not None else None

        def _render() -> tuple[bytes | None, bytes | None]:
            return _one(args), _one(hargs)

        async def _job() -> None:
            try:
                png, png_home = await self.hass.async_add_executor_job(_render)
            except Exception as err:  # noqa: BLE001 - a render failure must never break the poll
                _LOGGER.warning("AnyVac: reach mask render failed for %s: %s", device.name, err)
                png = png_home = None
            finally:
                self._reach_busy.discard(duid)
            self._reach_png[duid] = png
            self._reach_png_home[duid] = png_home
            self._reach_updated[duid] = dt_util.utcnow()
            self.async_update_listeners()

        self.hass.async_create_task(_job())

    def _reach_entity_id(self, duid: str, home: bool = False) -> str | None:
        try:
            from homeassistant.helpers import entity_registry as er

            uid = f"{duid}_anyvac_reach_home" if home else f"{duid}_anyvac_reach"
            return er.async_get(self.hass).async_get_entity_id("image", DOMAIN, uid)
        except Exception:  # noqa: BLE001 - registry not ready (tests, early start)
            return None

    def reach_png(self, duid: str, home: bool = False) -> bytes | None:
        return (self._reach_png_home if home else self._reach_png).get(duid)

    def reach_updated(self, duid: str) -> datetime | None:
        return self._reach_updated.get(duid)

    def reset_learning(
        self,
        duid: str | None = None,
        room: str | None = None,
        kind: str | None = None,
        estimates: bool = True,
        baselines: bool = True,
    ) -> None:
        """Prune learned data (anyvac.reset_learning) — e.g. estimate entries poisoned
        before the evidence-based typing fix. ``baselines`` (name kept for existing
        automations) now clears the persisted per-room completion % (docs/45 —
        there are no learned coverage baselines any more). Filters: vacuum duid,
        room name, kind (dry/wet); omitted = all."""
        kinds = [kind] if kind in ("dry", "wet") else ["dry", "wet"]

        def _prune(table: dict[str, dict[str, dict[str, Any]]]) -> None:
            for dd, rooms in table.items():
                if duid and dd != duid:
                    continue
                for rnm in list(rooms):
                    if room and rnm != room:
                        continue
                    for k in kinds:
                        rooms[rnm].pop(k, None)
                    if not rooms[rnm]:
                        rooms.pop(rnm)

        if estimates:
            _prune(self._estimates)
            self._delay_save(self._est_store, lambda: self._estimates, 2)
        if baselines:
            # `_room_coverage` has no duid dimension (mirrors `_history`'s cross-fleet
            # room-name keying), so a duid-scoped reset still clears by room/kind only.
            for rnm in list(self._room_coverage):
                if room and rnm != room:
                    continue
                for k in kinds:
                    self._room_coverage[rnm].pop(k, None)
                    self._room_coverage[rnm].pop(f"{k}_floor", None)
                if not self._room_coverage[rnm]:
                    self._room_coverage.pop(rnm)
            self._delay_save(self._cov_pct_store, lambda: self._room_coverage, 2)
        self.async_update_listeners()

    def pct_to_mm(self, duid: str, x_pct: float, y_pct: float) -> tuple[int, int] | None:
        """Convert a click given as PERCENT of the rendered map image into vacuum mm.

        Kontrakt v2 (docs/14 §3.6): the card sends clicks as image percentages to
        ``anyvac.goto`` / ``anyvac.zone_clean``; the pct→px→mm conversion lives here.
        Returns (x_mm, y_mm) or None when the vacuum/calibration is unavailable.
        """
        device = (self.data or {}).get(duid)
        if device is None:
            return None
        aff = _solve_affine(device.data.get("calibration_points"))
        dims = device.data.get("image_dims") or {}
        width, height = dims.get("width"), dims.get("height")
        scale = dims.get("scale") or 1
        if aff is None or not width or not height:
            return None
        # Percent → rendered-image px (the same px space as calibration_points).
        u = x_pct / 100.0 * width * scale
        v = y_pct / 100.0 * height * scale
        a, b, c, d, e, f = aff
        det = a * e - b * d
        if abs(det) < 1e-12:
            return None
        x = (e * (u - c) - b * (v - f)) / det
        y = (-d * (u - c) + a * (v - f)) / det
        return round(x), round(y)

    def px_per_mm(self, duid: str) -> float | None:
        """Linear mm→px scale factor from the vacuum's calibration affine
        transform (docs/37 §5) — for sizing a physical measurement (e.g. a
        drawn stroke width) in rendered-image pixels. Geometry stays owned by
        the coordinator (docs/14 §1); callers must not solve the affine
        transform themselves. None when calibration is unavailable — the
        caller falls back to a fixed pixel width.
        """
        device = (self.data or {}).get(duid)
        if device is None:
            return None
        aff = _solve_affine(device.data.get("calibration_points"))
        if aff is None:
            return None
        a, b, c, d, e, f = aff
        det = a * e - b * d
        return math.sqrt(abs(det)) if det else None

    def raw_map_for(self, duid: str) -> tuple[bytes, dict[str, Any]] | None:
        """Fresh raw Roborock map bytes for one duid (docs/40 Fáze 0), for
        ``anyvac.dump_raw_map``. Walks the SAME piggyback tree `_extract_device`
        does (via `_resolve_map_content` — no second implementation), but does it
        fresh on every call rather than reading `self.data`: raw bytes are
        deliberately never cached on the published device data (that would put
        them one accidental `**device.data` away from a sensor attribute /
        websocket payload). Returns ``(raw_bytes, meta)`` where meta carries
        ``map_flag``/``map_index``/``map_sequence``, or None when no Roborock v1
        coordinator for this duid has raw bytes available yet.
        """
        for rb_entry in self.hass.config_entries.async_entries(ROBOROCK_DOMAIN):
            runtime = getattr(rb_entry, "runtime_data", None)
            for coord in getattr(runtime, "v1", None) or []:
                if getattr(coord, "duid", None) != duid:
                    continue
                map_content = _resolve_map_content(coord)
                raw = getattr(map_content, "raw_api_response", None) if map_content else None
                if not isinstance(raw, (bytes, bytearray)):
                    return None
                home = getattr(getattr(coord, "properties_api", None), "home", None)
                current = getattr(home, "current_map_data", None) if home else None
                map_data = getattr(map_content, "map_data", None)
                add_params = getattr(map_data, "additional_parameters", None) if map_data else None
                meta: dict[str, Any] = {
                    "map_flag": getattr(current, "map_flag", None),
                    "map_index": add_params.get("map_index") if isinstance(add_params, dict) else None,
                    "map_sequence": add_params.get("map_sequence") if isinstance(add_params, dict) else None,
                }
                return bytes(raw), meta
        return None

    def roborock_coordinator_for(self, duid: str) -> Any | None:
        """The Roborock v1 coordinator of one duid (same piggyback walk as
        `raw_map_for`)."""
        for rb_entry in self.hass.config_entries.async_entries(ROBOROCK_DOMAIN):
            runtime = getattr(rb_entry, "runtime_data", None)
            for coord in getattr(runtime, "v1", None) or []:
                if getattr(coord, "duid", None) == duid:
                    return coord
        return None

    def roborock_status_for(self, duid: str) -> Any | None:
        """The Roborock `StatusTrait` of one duid (docs/47 §3, for
        ``anyvac.dock_resolve_error``)."""
        coord = self.roborock_coordinator_for(duid)
        return getattr(getattr(coord, "properties_api", None), "status", None)

    @property
    def view_layers(self) -> dict[str, bool]:
        """Shared dry/wet layer visibility for the card (synced across devices)."""
        return dict(self._view_layers)

    def set_layers(self, dry: bool | None = None, wet: bool | None = None) -> None:
        """Mutate the shared layer visibility (anyvac.set_layers)."""
        if dry is not None:
            self._view_layers["dry"] = bool(dry)
        if wet is not None:
            self._view_layers["wet"] = bool(wet)
        self._delay_save(self._layers_store, lambda: dict(self._view_layers), 2)
        self.async_update_listeners()

    @property
    def floorplan_seats(self) -> dict[str, dict[str, Any]]:
        """Visual editor override layer (docs/41 §4.6 + docs/42 appearance/
        rooms/room_style extensions), keyed by floorplan `src`. Each per-vacuum
        entry carries an optional "map" key, an optional "appearance" key and an
        optional "rooms" key, independently — any subset, or (defensively)
        none. The top-level entry ALSO carries its own card-level "rooms" key
        (sibling of "image_base"), for merged mode — set/cleared per room_key
        via a `set_floorplan_seat` call with `vacuum` omitted, same "vacuum
        given = per-vacuum, omitted = card-level" split `image_base` already
        uses (docs/42 §4.4) — and a card-level-only "room_style" key (fáze I
        addendum, §9), for the Rooms tool's global border-width sliders."""
        return {
            src: {
                "vacuums": {
                    ent: {
                        **({"map": dict(v["map"])} if "map" in v else {}),
                        **({"appearance": dict(v["appearance"])} if "appearance" in v else {}),
                        **(
                            {"rooms": {rk: dict(rv) for rk, rv in v["rooms"].items()}}
                            if "rooms" in v
                            else {}
                        ),
                    }
                    for ent, v in entry.get("vacuums", {}).items()
                },
                "image_base": dict(entry["image_base"]) if entry.get("image_base") else None,
                **(
                    {"rooms": {rk: dict(rv) for rk, rv in entry["rooms"].items()}}
                    if entry.get("rooms")
                    else {}
                ),
                **(
                    {"room_style": dict(entry["room_style"])}
                    if entry.get("room_style")
                    else {}
                ),
                "updated": entry.get("updated"),
            }
            for src, entry in self._floorplan_seats.items()
        }

    def set_floorplan_seat(
        self,
        floorplan: str,
        vacuum: str | None = None,
        map: dict[str, float] | None = None,
        image_base: dict[str, Any] | None = None,
        appearance: dict[str, Any] | None = None,
        rooms: dict[str, dict[str, Any] | None] | None = None,
        room_style: dict[str, Any] | None = None,
    ) -> None:
        """Set or clear a manual floorplan/appearance/rooms/room_style override
        (anyvac.set_floorplan_seat, docs/41 §4.6 + docs/42 appearance + rooms +
        room_style extensions).

        `vacuum` given → this call is about that vacuum's per-vacuum entry.
        `map` and `appearance` are two fully independent nullable fields on
        that entry, symmetric with each other: ``None`` clears just that
        part of the override, a dict sets it — exactly like `map` always
        worked, now also true for `appearance`. The card always sends both
        together on every Save (the current draft state for each), so a
        partial update from the UI never happens in practice, but the API
        itself does not assume that — a caller sending only one of the two
        legitimately clears the other.

        `rooms` is DIFFERENT on purpose (docs/42 §4.4, fáze K): it is a MAP of
        room_key -> (room override dict | ``None``), not a single atomic
        value like `map`/`appearance`. Each room_key in that map is merged
        independently into this vacuum's existing stored `rooms` map — a dict
        value sets/replaces just that one room's override, ``None`` clears
        just that one room's override, and any room_key this vacuum already
        has an override for but that isn't mentioned in THIS call is left
        completely untouched. `rooms` omitted entirely from a call (its
        Python default, ``None``) means "this call doesn't touch rooms at
        all" — unlike `map`/`appearance`, omitting `rooms` never clears
        anything. This asymmetry is deliberate: `map`/`appearance` are each
        one whole-record edit the card always resends in full on every Save,
        while Rooms-tool edits are inherently per-room (drag one room, delete
        another) and forcing every unrelated room's override to be resent on
        every single-room edit would be exactly the kind of accidental-wipe
        footgun the "no sentinel" `map`/`appearance` design has to work
        around by resending everything — rooms avoids it structurally
        instead, by keying the merge.

        `vacuum` omitted → this call is about the card-level entry instead:
        `image_base` (``None`` clears it, same atomic "no sentinel" contract
        as `map`/`appearance` — a caller that cares about an existing
        `image_base` override must resend it in the same call or it's
        cleared), `room_style` (fáze I addendum, §9 — the SAME atomic "no
        sentinel" contract as `image_base`: it is a whole-record override,
        not per-key like `rooms`, since it only ever has two fields the card
        always edits/sends together, mirroring `appearance`'s "card always
        sends both together on every Save" precedent) and/or `rooms` (same
        per-room_key merge/omit-is-untouched semantics described above, just
        stored as a sibling of `image_base` on the floorplan entry itself
        instead of nested under one vacuum — docs/42 §4.4: "stejná vacuum/
        card-level volba jako u map"). `map`/`appearance` are ignored when
        `vacuum` is omitted — those two only ever have per-vacuum meaning,
        unlike `rooms`/`image_base`/`room_style`. `room_style` is likewise
        ignored when `vacuum` IS given — it is a global setting (applies to
        every vacuum's rooms), there is no per-vacuum `room_style`. A caller
        never mixes `vacuum`+`map`/`appearance` with a card-level-only call
        in practice (the card's tools are per-vacuum XOR card-level), but a
        `rooms`-only (or `room_style`-only) card-level Save (e.g. the Rooms
        tool editing one room's geometry, or just dragging a border-width
        slider) DOES still clear `image_base`/`room_style` if either isn't
        resent, by the same "no sentinel" logic — `image_base`, `room_style`
        and `rooms` are independent of EACH OTHER within this branch, but
        none of the three gets a free pass from its own resend-to-keep rule
        just because a different one was the one being edited; the card's
        Rooms tool Save handler is responsible for always resending the
        other two's current draft alongside whichever one actually changed
        (same discipline `_alignSave` already applies to `map`+`appearance`).
        `image_base`'s own shape (crop_box/home_anchors, phase G) is opaque
        here: stored and returned as-is, never interpreted.
        """
        floorplan = str(floorplan)
        if vacuum:
            vacuum = str(vacuum)
            entry = self._floorplan_seats.setdefault(
                floorplan, {"vacuums": {}, "image_base": None}
            )
            vac_entry = dict(entry["vacuums"].get(vacuum) or {})
            if map is None:
                vac_entry.pop("map", None)
            else:
                rounded: dict[str, float] = {
                    "rotation": round(float(map["rotation"]), 2),
                    "scale": round(float(map["scale"]), 2),
                    "offset_x": round(float(map["offset_x"]), 2),
                    "offset_y": round(float(map["offset_y"]), 2),
                }
                if map.get("scale_y") is not None:
                    rounded["scale_y"] = round(float(map["scale_y"]), 2)
                vac_entry["map"] = rounded
            if appearance is None:
                vac_entry.pop("appearance", None)
            else:
                # Already validated by SET_FLOORPLAN_SEAT_SCHEMA's
                # _FLOORPLAN_SEAT_APPEARANCE_SCHEMA before this is called —
                # stored as-is, no further coercion/rounding here.
                vac_entry["appearance"] = dict(appearance)
            if rooms is not None:
                stored_rooms = _merge_room_overrides(vac_entry.get("rooms"), rooms)
                if stored_rooms:
                    vac_entry["rooms"] = stored_rooms
                else:
                    vac_entry.pop("rooms", None)
            if vac_entry:
                entry["vacuums"][vacuum] = vac_entry
            else:
                entry["vacuums"].pop(vacuum, None)
        else:
            entry = self._floorplan_seats.setdefault(
                floorplan, {"vacuums": {}, "image_base": None}
            )
            if image_base is None:
                entry["image_base"] = None
            else:
                entry["image_base"] = dict(image_base)
            if rooms is not None:
                # docs/42 §4.4 — card-level `rooms` (merged mode): same
                # per-room_key merge as the per-vacuum branch above, just
                # stored as a sibling of "image_base" instead of nested under
                # one vacuum's own entry. `vacuum` omitted is what selects
                # this branch (§4.4: "stejná vacuum/card-level volba jako u
                # map"), same as `image_base` already works.
                stored_rooms = _merge_room_overrides(entry.get("rooms"), rooms)
                if stored_rooms:
                    entry["rooms"] = stored_rooms
                else:
                    entry.pop("rooms", None)
            # docs/42 §3.3/§9 (fáze I addendum) — same atomic "no sentinel"
            # contract as `image_base` immediately above: whole-record,
            # `None`/omitted clears it.
            if room_style is None:
                entry.pop("room_style", None)
            else:
                # Already validated by SET_FLOORPLAN_SEAT_SCHEMA's
                # _FLOORPLAN_SEAT_ROOM_STYLE_SCHEMA before this is called.
                entry["room_style"] = dict(room_style)
        # Prune once an entry holds nothing at all — otherwise renaming or
        # removing floorplans over time would leave an ever-growing pile of
        # dead `src` keys in the store.
        entry = self._floorplan_seats.get(floorplan)
        if entry is not None:
            if (
                not entry.get("vacuums")
                and not entry.get("image_base")
                and not entry.get("rooms")
                and not entry.get("room_style")
            ):
                self._floorplan_seats.pop(floorplan, None)
            else:
                entry["updated"] = dt_util.utcnow().isoformat(timespec="seconds")
        self._delay_save(self._seats_store, lambda: self._floorplan_seats, 2)
        self.async_update_listeners()

    @property
    def room_pins(self) -> dict[str, dict[str, str]]:
        """Per-room, per-pass vacuum pins {room_name: {"dry"/"wet": vacuum ref}}
        (docs/18 §7e; per-kind since 2026-07-25)."""
        return {room: dict(kinds) for room, kinds in self._room_pins.items()}

    def set_room_pin(
        self, room: str, vacuum: str | None = None, kind: str | None = None
    ) -> None:
        """Pin a room's dry or wet pass to a vacuum (anyvac.pin_room).

        ``kind`` omitted unpins the room entirely (both passes) — used when a
        room is deselected on the dashboard, so there's always a simple way
        back to fully automatic assignment. ``kind`` given with no/empty
        ``vacuum`` unpins just that one pass, leaving the other untouched.
        """
        room = str(room)
        if kind is None:
            self._room_pins.pop(room, None)
        elif vacuum:
            self._room_pins.setdefault(room, {})[kind] = str(vacuum)
        else:
            kinds = self._room_pins.get(room)
            if kinds is not None:
                kinds.pop(kind, None)
                if not kinds:
                    self._room_pins.pop(room, None)
        self._delay_save(self._pins_store, lambda: dict(self._room_pins), 2)
        self.async_update_listeners()

    @property
    def room_sequence(self) -> dict[str, int]:
        """Kept for older cards (docs/19): the hand-edited order is gone (docs/52
        O1); the app's own order is read from each robot (`app_sequence`)."""
        return {}

    async def async_refresh_app_sequence(self, duid: str, force: bool = False) -> dict[str, int]:
        """Read the room order set in the Roborock app from the robot
        (`get_clean_sequence`, docs/52 O1). Cached for an hour; any failure
        keeps what we had — the order is a nicety, never a reason to fail."""
        at = self._app_sequence_at.get(duid)
        if not force and at is not None and (dt_util.utcnow() - at).total_seconds() < APP_SEQUENCE_TTL_S:
            return dict(self.app_sequence.get(duid) or {})
        rb = self.roborock_coordinator_for(duid)
        api = getattr(rb, "properties_api", None)
        channel = getattr(getattr(api, "map_content", None), "rpc_channel", None)
        if channel is None:
            channel = getattr(getattr(api, "command", None), "rpc_channel", None)
        if channel is None:
            return dict(self.app_sequence.get(duid) or {})
        try:
            from roborock.roborock_typing import RoborockCommand  # official integration's library

            resp = await channel.send_command(RoborockCommand.GET_CLEAN_SEQUENCE)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("AnyVac: get_clean_sequence for %s failed: %s", duid, err)
            return dict(self.app_sequence.get(duid) or {})
        segs = _clean_sequence_segments(resp)
        dev = (self.data or {}).get(duid)
        names = {
            r.get("segment_id"): r.get("name")
            for r in ((getattr(dev, "data", None) or {}).get("rooms") or [])
        }
        seq: dict[str, int] = {}
        for seg in segs:
            nm = names.get(seg)
            if nm and nm not in seq:
                seq[nm] = len(seq) + 1
        self.app_sequence[duid] = seq
        self._app_sequence_at[duid] = dt_util.utcnow()
        return dict(seq)

    # -- docs/44 F2: job progress (read by the card's hero bar) -----------------

    def set_job_state(self, state: dict[str, Any] | None) -> None:
        """The running job, as the job runner holds it (docs/52 §9.4):
        ``{job_id, started_at, mode, queues: {duid: {vacuum, batch, items:
        [{id, room, rooms, kind, state, est_min, reason, manual, ...}],
        wash: {...}}}, warnings}``. None when no job runs. The runner owns
        the queues; the coordinator only turns them into `job_progress`."""
        self._job_state = state
        self.async_update_listeners()

    def _live_robot_model(self, duid: str, q: dict[str, Any]) -> dict[str, Any]:
        """`planner.simulate_queues` input for one robot of the running job:
        the items still to run, the active one reduced to what is left of it."""
        dev = (self.data or {}).get(duid)
        dd = getattr(dev, "data", None) or {}
        items: list[dict[str, Any]] = []
        prev_kind: str | None = None
        for it in q.get("items") or []:
            st = it.get("state")
            if st in ("done", "blocked", "removed"):
                prev_kind = it.get("kind")
                continue
            est = float(it.get("est_min") or 0.0)
            if st == "active":
                est *= 1 - min(self._item_pct(dd, it), 100) / 100
            items.append({"id": it["id"], "kind": it.get("kind"), "rooms": it.get("rooms") or [], "est_min": est})
        wash = q.get("wash") or {}
        return {
            "items": items,
            "wash_mode": wash.get("mode"),
            "interval": wash.get("interval_min"),
            "batch": bool(q.get("batch")),
            "since_wash": wash.get("mop_min_since_wash") or 0.0,
            "prev_kind": prev_kind,
        }

    @staticmethod
    def _item_pct(dd: dict[str, Any], it: dict[str, Any]) -> int:
        """Live % of an active item from the room-completion tracker."""
        prog = dd.get("rooms_progress") or {}
        vals = []
        for room in it.get("rooms") or []:
            raw = (prog.get(room) or {}).get(f"{it.get('kind')}_pct")
            vals.append(int(raw) if isinstance(raw, (int, float)) else 0)
        return round(sum(vals) / len(vals)) if vals else 0

    @property
    def job_progress(self) -> dict[str, Any]:
        """Card-facing snapshot of the running job (docs/44 F2, v2 docs/52 §9.4).

        The card never estimates time itself (docs/14): every item's start and
        finish come from `planner.simulate_queues` over what is left — the same
        model the plan was made with — so an edit, a slow room or a blocked one
        moves the finish time at once."""
        from .planner import simulate_queues  # local: planner imports nothing from here

        st = self._job_state
        if not st:
            return {"active": False}
        now = dt_util.utcnow()
        data = self.data or {}
        queues = st.get("queues") or {}
        robots = {duid: self._live_robot_model(duid, q) for duid, q in queues.items()}
        times = simulate_queues(robots)
        left = max((f for _, f in times.values()), default=0.0)

        def at(minutes: float) -> str:
            return (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")

        out_rows: list[dict[str, Any]] = []
        out_queues: dict[str, dict[str, Any]] = {}
        vacs: dict[str, dict[str, Any]] = {}
        for duid, q in queues.items():
            dd = getattr(data.get(duid), "data", None) or {}
            entity = q.get("vacuum")
            qitems: list[dict[str, Any]] = []
            for it in q.get("items") or []:
                state = it.get("state")
                if state == "removed":
                    continue
                pct = 100 if state == "done" else (self._item_pct(dd, it) if state == "active" else 0)
                start_finish = times.get(it["id"])
                row = {
                    "id": it["id"], "room": it.get("room"), "rooms": it.get("rooms"),
                    "kind": it.get("kind"), "state": state, "pct": pct,
                    "est_min": it.get("est_min"), "manual": bool(it.get("manual")),
                    "start_at": at(start_finish[0]) if start_finish else None,
                    "finish_at": at(start_finish[1]) if start_finish else it.get("done_at"),
                }
                if it.get("reason"):
                    row["reason"] = it["reason"]
                qitems.append(row)
                # v1 rows: queued / active / done (blocked kept as its own word).
                legacy = {"sent": "active", "queued": "queued"}.get(state, state)
                for room in it.get("rooms") or []:
                    out_rows.append({
                        "room": room, "kind": it.get("kind"), "vacuum": entity,
                        "state": legacy, "pct": pct,
                    })
            v = vacs.setdefault(entity, {"room": None, "kind": None, "pct": None, "next_room": None})
            for r in qitems:
                if r["state"] in ("active", "sent") and v["room"] is None:
                    v.update(room=r["room"], kind=r["kind"], pct=r["pct"])
                elif r["state"] == "queued" and v["next_room"] is None:
                    v["next_room"] = r["room"]
            out_queues[entity] = {
                "mode": "batch" if q.get("batch") else "rooms",
                "state": q.get("phase") or "idle",
                "items": qitems,
                "wash": dict(q.get("wash") or {}),
                "planned_stops": [],
            }
        rooms = {r["room"] for r in out_rows}
        rooms_done = {
            rm for rm in rooms if all(r["state"] == "done" for r in out_rows if r["room"] == rm)
        }
        started = st.get("started_at")
        return {
            "active": True,
            "schema": 2,
            "job_id": st.get("job_id"),
            "started_at": started.isoformat(timespec="seconds") if hasattr(started, "isoformat") else started,
            "finish_at": at(left),
            "eta_min_left": round(left, 1),
            "rooms_total": len(rooms),
            "rooms_done": len(rooms_done),
            "passes_total": len(out_rows),
            "passes_done": sum(1 for r in out_rows if r["state"] == "done"),
            "rooms": out_rows,
            "vacuums": vacs,
            "queues": out_queues,
            # docs/49 A5: pre-clean settings that did not take (e.g. the dry run's
            # `mop_intensity: off`) — the run was NOT blocked, the card may say so.
            "warnings": list(st.get("warnings") or []),
        }

    def add_job_warning(self, text: str) -> None:
        """Note a problem of the running job (shown in `job_progress.warnings`)."""
        st = self._job_state
        if st is None:
            return
        warnings = st.setdefault("warnings", [])
        if text not in warnings:
            warnings.append(text)
            self.async_update_listeners()

    def save_job(self, snapshot: dict[str, Any] | None) -> None:
        """Persist the running job (None = no job any more)."""
        self._job_saved = snapshot
        self._delay_save(self._job_store, lambda: self._job_saved or {}, 2)

    async def async_load_job(self) -> dict[str, Any] | None:
        data = await self._job_store.async_load()
        return data if isinstance(data, dict) and data.get("job_id") else None

    # -- service points (docs/52 §5 item 7) -----------------------------------

    def service_points(self, duid: str) -> list[dict[str, Any]]:
        """This vacuum's named points, robot mm, in the order they were made."""
        return [dict(p) for p in self._service_points.get(duid, [])]

    def set_service_point(self, duid: str, name: str, x: float, y: float) -> None:
        """Add or replace (same name) a named point. Raises ValueError when the
        name is empty/too long or the vacuum already has SERVICE_POINTS_MAX."""
        name = name.strip()
        if not name or len(name) > SERVICE_POINT_NAME_MAX:
            raise ValueError(f"name must be 1-{SERVICE_POINT_NAME_MAX} characters")
        points = self._service_points.setdefault(duid, [])
        point = {"name": name, "x": int(round(x)), "y": int(round(y))}
        for i, p in enumerate(points):
            if p["name"] == name:
                points[i] = point
                break
        else:
            if len(points) >= SERVICE_POINTS_MAX:
                raise ValueError(f"a vacuum can hold at most {SERVICE_POINTS_MAX} service points")
            points.append(point)
        self._save_service_points()

    def remove_service_point(self, duid: str, name: str | None = None) -> list[str]:
        """Remove one named point, or all of the vacuum's points (name None).
        Returns the names removed."""
        points = self._service_points.get(duid, [])
        if name is None:
            removed = [p["name"] for p in points]
            self._service_points.pop(duid, None)
        else:
            removed = [p["name"] for p in points if p["name"] == name]
            rest = [p for p in points if p["name"] != name]
            if rest:
                self._service_points[duid] = rest
            else:
                self._service_points.pop(duid, None)
        if removed:
            self._save_service_points()
        return removed

    def _save_service_points(self) -> None:
        self._delay_save(self._sp_store, lambda: self._service_points, 1)
        # Publish at once rather than on the next poll (up to 30 s away).
        for device in (self.data or {}).values():
            aff = _solve_affine(device.data.get("calibration_points"))
            device.data["service_points"] = self._published_service_points(device, aff)
        self.async_update_listeners()

    def _published_service_points(
        self, device: AnyVacDevice, aff: tuple[float, ...] | None
    ) -> list[dict[str, Any]]:
        """`service_points` attribute: robot mm plus the same point in the
        robot's image px (v2), the home frame px (v3) and the room it lies in."""
        out: list[dict[str, Any]] = []
        for p in getattr(self, "_service_points", {}).get(device.duid, []):
            mm = {"x": p["x"], "y": p["y"]}
            home = self.mm_to_home_px(device.duid, p["x"], p["y"])
            out.append({
                **p,
                "px": _px_point(mm, aff) if aff is not None else None,
                "home_px": {"x": round(home[0], 1), "y": round(home[1], 1)} if home else None,
                "room": _room_at(device.data.get("rooms") or [], p["x"], p["y"]),
            })
        return out

    def set_task_item(self, duid: str, rooms: set[str] | None) -> None:
        """The room(s) of the queue item a job just sent to this robot — the
        measurement targets of the task (docs/52 §9.3). None = not known."""
        if rooms:
            self._task_item[duid] = set(rooms)
        else:
            self._task_item.pop(duid, None)

    def discard_task(self, duid: str) -> None:
        """Forget what the running task measured without learning from it: the
        job removed the room mid-clean (docs/52 §3.4) — an interrupted room
        must never calibrate."""
        self._room_elapsed[duid] = {}
        self._runs[duid] = covmod.RunTracker()
        self._run_targets_seen.pop(duid, None)
        self._session_confirmed[duid] = set()
        self._session_rooms[duid] = set()
        self._task_open[duid] = False

    def set_job_rooms(self, duid: str, rooms: set[str] | None, keep_id: bool = False) -> None:
        """Plan-scope transit labeling (docs/17 §1.3) — called by services.py's
        _JobRunner when an orchestrated job (anyvac.clean) starts/finishes for this
        vacuum. `rooms` = the full room-name scope of the job for this duid (union of
        its dry + wet assignment); `None` clears it. Not persisted and does not touch
        `async_update_listeners()` — this is a side-channel for the next poll's
        `_attribute_points`, not user-facing sensor state."""
        if rooms is None:
            self._job_rooms.pop(duid, None)
            self._task_item.pop(duid, None)
        else:
            # An edit of the running job (docs/52 §3.4) widens the scope but is
            # still the same job: the drawn trace keeps stitching (docs/27).
            if not (keep_id and duid in self._job_rooms):
                self._job_seq += 1
                self._job_id[duid] = self._job_seq
            self._job_rooms[duid] = set(rooms)

    def _sortie_is_new_job(self, duid: str) -> bool:
        """Cross-sortie path stitching (docs/27), corrected 2026-07-26: tells
        the two path-wipe call sites below whether the sortie that's about to
        start belongs to a job the stored trace hasn't seen yet.

        True (wipe) when either: (a) no job scope is active right now — the
        original pre-docs/27 case, manual/degraded/raw `run_job` starts, wiped
        on every sortie as before; or (b) a job IS active, but its id differs
        from the one the currently stored trace was last stamped with — a
        genuinely NEW `anyvac.clean` job, whose id `set_job_rooms` assigned
        fresh, distinct from whatever job (if any) the old trace belonged to.

        False (keep, just close the segment) only when the active job's id
        matches what the stored trace already belongs to — a sortie restart
        WITHIN the same job (progressive pool dispatch, dock trip between a
        dry and wet pass, docs/23), which is the one case docs/27 exists to
        stitch instead of drawing a straight line across the dock trip.

        Has the side effect of stamping the trace with the current job id, so
        each detected sortie-reset must call this exactly once."""
        current = self._job_id.get(duid) if duid in self._job_rooms else None
        is_new = current is None or current != self._path_job_id.get(duid)
        self._path_job_id[duid] = current
        return is_new

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
        self._delay_save(self._sel_store, lambda: sorted(self._selected_rooms), 2)
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
        self._delay_save(self._est_store, lambda: self._estimates, 5)
        return before, after

    def _update_geometry(self, device: AnyVacDevice) -> covmod.Geometry | None:
        """Coverage geometry for one vacuum (docs/45 §2.1): room floor decoded
        from the robot's own raw map, reachable part only. Rebuilt only when the
        map IMAGE or the room list changes — the raw blob itself changes on every
        poll while cleaning (the trajectory lives in it), so the key is the
        decoded room-id grid, not the raw hash. Falls back to bounding boxes when
        the raw map is unavailable or the decoder's self-test fails, so the rest
        of the pipeline never needs a second code path."""
        duid = device.duid
        rooms = device.data.get("rooms") or []
        names = tuple(sorted((str(r.get("segment_id")), r.get("name") or "") for r in rooms))
        dbg = device.data.get("debug_map") if isinstance(device.data.get("debug_map"), dict) else {}
        raw_sha1 = dbg.get("raw_sha1") if dbg else None
        cached = self._geo.get(duid)
        if cached is not None and cached[0] == (raw_sha1, names):
            return cached[2]
        grid = None
        if raw_sha1 is not None and duid not in self._home_frame_disabled:
            try:
                fetched = self.raw_map_for(duid)
                if fetched is not None:
                    candidate = homeframe.decode_grid(fetched[0])
                    lib_rooms = dict(device.data.get("_lib_rooms") or {})
                    if not lib_rooms or homeframe.grid_self_test(candidate, lib_rooms):
                        grid = candidate
            except Exception as err:  # noqa: BLE001 - geometry must never break the poll
                _LOGGER.debug("AnyVac: coverage grid unavailable for %s: %s", device.name, err)
        if grid is not None:
            gkey = (
                "grid", grid.left, grid.top, grid.width, grid.height,
                hashlib.sha1(grid.room_id.tobytes()).hexdigest(), names,
            )
        else:
            gkey = ("bbox", tuple((r.get("name"), r.get("x0"), r.get("y0"), r.get("x1"), r.get("y1")) for r in rooms))
        if cached is not None and cached[1] == gkey:
            geo = cached[2]
        else:
            geo = (
                covmod.geometry_from_grid(grid, rooms)
                if grid is not None
                else covmod.geometry_from_bboxes(rooms)
            )
        self._geo[duid] = ((raw_sha1, names), gkey, geo)
        return geo

    def _next_room(
        self, run: covmod.RunTracker, targets: set[str] | None, duid: str | None = None
    ) -> str | None:
        """The room the configured sequence (docs/19) says the robot cleans next:
        the lowest-numbered target not yet finished or active. None when the
        sequence doesn't know any of the remaining targets."""
        seq = self.app_sequence.get(duid) or {} if duid else {}
        pool = targets if targets is not None else set(seq)
        left = [r for r in pool if r not in run.done and r != run.active]
        if targets is not None and len(left) == 1:
            return left[0]  # the only room left to clean is next, sequence or not
        ordered = [r for r in left if r in seq]
        return min(ordered, key=lambda r: seq[r]) if ordered else None

    def _run_targets(self, device: AnyVacDevice) -> set[str] | None:
        """Rooms this run was asked to clean (docs/45 §2.2): the firmware's own
        list of segments of the running clean (map BLOCKS) — present for every
        segment clean however it was started — united with an orchestrated
        job's scope. None = unknown (full-home clean): every room is a target."""
        duid = device.duid
        seg_names = {
            str(r.get("segment_id")): r.get("name") for r in device.data.get("rooms", [])
        }
        segs = device.data.get("target_segments") or []
        names = {seg_names.get(str(s)) for s in segs} - {None}
        # The item the job runner sent (docs/52 §9.3) — one room per task. A
        # job without per-item information falls back to its whole scope.
        item = self._task_item.get(duid)
        job = self._job_rooms.get(duid)
        if item:
            names |= set(item)
        elif job:
            names |= set(job)
        if names:
            self._run_targets_seen[duid] = set(names) | self._run_targets_seen.get(duid, set())
        return set(self._run_targets_seen.get(duid) or ()) or None

    def _attribute_points(self, device: AnyVacDevice, tail_only: bool = False) -> None:
        """Point attribution (docs/16 §3, rewritten for docs/45).

        Slice the newly-added trajectory points per layer and hand them to the
        run's `RunTracker`, which decides which room is really being cleaned
        (targets + sequence + dwell) and stamps the robot footprint there. The
        per-room path length it credits splits this poll's time delta, so the
        30 s polling interval stops mattering for accuracy and pauses (no new
        points) attribute nothing.

        The poll that ENDS active cleaning (return, mop-wash trip, run end) is
        still attributed — restricted to the active room: the firmware refreshes
        the map on that very state change, so its new points are the last
        seconds of cleaning (docs/45 §1.2 — dropping them cost the last room of
        every sortie 15-25 %).

        ``tail_only`` = called from `_track_and_emit` on the sortie's falling
        edge, right before a possible harvest: take the tail, but never read a
        shorter trajectory as a path reset there — a reset verdict could wipe
        the run the harvest is about to read. The regular call later in the
        same poll handles any genuine reset.
        """
        duid = device.duid
        now = dt_util.utcnow()
        last = self._last_poll.get(duid)
        # docs/47 §1.3/§1.5: time is measured between map SNAPSHOTS. A poll that
        # saw no new snapshot while the robot is actively cleaning leaves the
        # clock where it is — its time belongs to the points the next snapshot
        # brings. In any other state it advances as before (pauses drop out).
        fresh = self._is_map_fresh(duid)
        if fresh or device.data.get("status_state") not in ACTIVE_CLEAN_STATES:
            self._last_poll[duid] = now

        seen = self._path_seen.setdefault(duid, {"dry": 0, "wet": 0})
        layers = (("dry", "_path_dry"), ("wet", "_path_wet"))
        run = self._runs.setdefault(duid, covmod.RunTracker())

        # Slice out the new points per layer (handle a path reset) regardless of state.
        new_by_layer: dict[str, list[dict[str, float]]] = {}
        # Computed lazily, at most once per poll, and reused across both layers below —
        # `_sortie_is_new_job` stamps as a side effect, so calling it twice for the SAME
        # sortie-reset event (dry and wet both reset together at every sortie boundary)
        # would make the second call wrongly see "already stamped, not new".
        sortie_wipe: bool | None = None
        for layer, dkey in layers:
            full = device.data.get(dkey) or []
            start = seen.get(layer, 0)
            if tail_only and len(full) < start:
                new_by_layer[layer] = []
                continue
            if len(full) < start:  # path was reset/trimmed -> start this layer over
                start = 0
                # Cross-sortie stitching (docs/27, job-id corrected 2026-07-26): a
                # mid-job sortie restart (robot back from a dock trip, next
                # pool-dispatch batch) must NOT wipe the already-drawn trace — only
                # close the current segment so the next points start a new one. A
                # genuinely NEW job (or no job scope at all — degraded mode / manual
                # per-vacuum start / raw run_job) DOES wipe, same as before docs/27.
                if sortie_wipe is None:
                    sortie_wipe = self._sortie_is_new_job(duid)
                if sortie_wipe:
                    # docs/36: what the run measured follows the SAME verdict as the
                    # trace — a firmware path reset in the middle of a run keeps it.
                    run = self._runs[duid] = covmod.RunTracker()
                    self._run_targets_seen.pop(duid, None)
                run.break_path(layer)
                if layer == "dry":
                    if sortie_wipe:
                        self._dry_path[duid] = []
                    self._dry_path_open[duid] = False
                else:
                    if sortie_wipe:
                        self._wet_path[duid] = []
                    self._wet_path_open[duid] = False
                if sortie_wipe:
                    # Segment indexes are about to be reused by a different
                    # trace — drop the decimation memo with them (1.1.0).
                    self._decim_cache.get(duid, {}).pop(layer, None)
            new_by_layer[layer] = full[start:]
            seen[layer] = len(full)

        cleaning = bool(device.data.get("in_cleaning"))
        transit = bool(device.data.get("transit"))
        vacuuming = bool(device.data.get("vacuuming"))

        # Segmented dry trace (docs/14 §3.9): append the new trajectory points only when
        # the robot is genuinely cleaning AND vacuuming (suction on) — transit / mop-wash
        # / goto driving and mop-only passes must not render (or count) as the dry layer.
        # Gate just closed→open (or first points ever): start a NEW segment so the gap
        # left by the excluded points is never bridged by a straight line. Gate stayed
        # open across this poll (even one with zero new points, e.g. a paused robot):
        # keep extending the current segment.
        gate_open = cleaning and not transit and vacuuming
        was_dry_open = self._dry_path_open.get(duid, False)
        dry_changed = gate_open and bool(new_by_layer["dry"])
        if dry_changed:
            segs = self._dry_path.setdefault(duid, [])
            # `not segs`: the gate can open on a poll that brought no points yet
            # (no segment was started then) — the first points must still get one.
            if not was_dry_open or not segs:
                segs.append([])
            segs[-1].extend(new_by_layer["dry"])
        self._dry_path_open[duid] = gate_open
        # The gate closing (e.g. the session-end poll, cleaning -> not cleaning)
        # touches no NEW points at all, so `dry_changed` alone would miss it —
        # but it must still persist: otherwise a restart landing right after a
        # session ends (before any new sortie) would reload with the segment
        # incorrectly still marked "open" (2026-07-26, path persistence tests).
        dry_open_changed = was_dry_open != gate_open

        # Wet trace (docs/27): no semantic gate needed (see `_wet_path_open` docstring in
        # __init__) — a new segment opens whenever `_wet_path_open` was forced closed by a
        # job/session boundary above; otherwise new points just extend the open segment.
        wet_changed = bool(new_by_layer["wet"])
        if wet_changed:
            wsegs = self._wet_path.setdefault(duid, [])
            if not self._wet_path_open.get(duid):
                wsegs.append([])
            wsegs[-1].extend(new_by_layer["wet"])
            self._wet_path_open[duid] = True

        # Persist the trace (2026-07-26, see `_paths_store`) only when it actually
        # changed this poll — new points appended, an open/closed transition, or
        # a wipe/stitch decision was made — not on every idle poll, to avoid
        # writing to disk every SCAN_INTERVAL forever regardless of whether the
        # vacuum is doing anything.
        if dry_changed or wet_changed or dry_open_changed or sortie_wipe is not None:
            self._delay_save(self._paths_store,
                self._paths_for_save,
                _PATHS_WIPE_SAVE_DELAY_S if sortie_wipe else _PATHS_SAVE_DELAY_S,
            )

        # Coverage (docs/45). `cleaning_gate` = genuinely cleaning this poll; the
        # poll right after it closed is attributed once more, restricted to the
        # active room (see docstring).
        cleaning_gate = cleaning and not transit
        # A task closed on its return edge is learned only once the next map
        # snapshot brought its last seconds (`_maybe_harvest_pending`) — keep
        # attributing that tail, restricted to the room, until then.
        was_gate = self._cov_gate.get(duid, False) or duid in self._harvest_pending
        self._cov_gate[duid] = cleaning_gate
        if not cleaning_gate and not was_gate:
            for layer in ("dry", "wet"):
                run.break_path(layer)
            return
        if cleaning:
            rep = device.data.get("repeat")
            if isinstance(rep, int) and 1 <= rep <= 3:
                run.passes = rep
        geo = self._update_geometry(device)
        if geo is None or not geo.rooms:
            return
        targets = self._run_targets(device)

        def _xy(points: list[dict[str, float]]) -> list[tuple[float, float]]:
            return [
                (float(p["x"]), float(p["y"]))
                for p in points
                if p.get("x") is not None and p.get("y") is not None
            ]

        # A mop carriage that is down while the water is off leaves mop points
        # but no wet floor (S7 MaxV, docs/49 / docs/52 O3): no wet evidence.
        wet_pts = [] if device.data.get("water_off") else new_by_layer["wet"]
        weights = run.feed(
            geo,
            _xy(new_by_layer["dry"]),
            _xy(wet_pts),
            targets=targets,
            next_room=self._next_room(run, targets, duid),
            vacuuming=vacuuming,
            restrict=not cleaning_gate,
        )
        if not cleaning_gate:
            # The restricted tail poll adds coverage only. Its time delta stays
            # unattributed exactly as before docs/45 — the wall-clock gap may
            # already contain the start of a mop-wash trip.
            for layer in ("dry", "wet"):
                run.break_path(layer)
            return

        if last is None or not fresh:
            return
        delta = (now - last).total_seconds()
        if delta <= 0 or delta > 600:  # ignore restarts / large gaps
            return
        total = sum(weights.values())
        if not total:
            return  # no new cleaning points this poll → no time attributed
        e = self._room_elapsed.setdefault(duid, {})
        for nm, w in weights.items():
            e[nm] = e.get(nm, 0.0) + delta * (w / total)

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

        # Mid-clean transit (mop wash, bin empty, returning, goto): the robot drives
        # through / sits in rooms it is NOT cleaning. Freeze the debounce entirely —
        # neither confirm the room the dock is in, nor report the previous room done
        # (docs/13 A2: this used to break single-room calibration and released the wet
        # robot into rooms the dry robot had not finished).
        if cleaning and device.data.get("transit"):
            return

        if not cleaning:
            prev = self._confirmed_room.get(duid)
            if prev and self._was_cleaning.get(duid):
                # Fire done/stamp only with real cleaning evidence — a confirmed room
                # the robot merely drove through (dock in room) must not release the
                # orchestrator's wet pass nor stamp history.
                self._emit_room_done(device, prev, "docked")
            self._confirmed_room[duid] = None
            self._raw_room[duid] = None
            self._raw_count[duid] = 0
            return

        if raw == self._raw_room.get(duid):
            # docs/47 §1.3: "two consecutive polls" means two map SNAPSHOTS — a
            # second poll on the same snapshot must not confirm the room.
            if self._is_map_fresh(duid):
                self._raw_count[duid] = self._raw_count.get(duid, 0) + 1
        else:
            self._raw_room[duid] = raw
            self._raw_count[duid] = 1

        if raw and self._raw_count[duid] >= 2 and raw != self._confirmed_room.get(duid):
            prev = self._confirmed_room.get(duid)
            self._confirmed_room[duid] = raw
            self._session_confirmed.setdefault(duid, set()).add(raw)
            if prev:
                self._emit_room_done(device, prev, "left")

    def _emit_room_done(self, device: AnyVacDevice, room: str, reason: str) -> None:
        """Stamp history + fire ``anyvac_room_done`` for `room` — only with real
        cleaning evidence (a room merely driven through must neither release a
        wet pass nor stamp history), and once per task."""
        duid = device.duid
        fired = self._task_rooms_done.setdefault(duid, set())
        if room in fired:
            return
        kinds = self._evidence_kinds(device, room)
        if not kinds:
            _LOGGER.debug("AnyVac: %s left %s without evidence — no room_done", device.name, room)
            return
        fired.add(room)
        self._stamp_room(device, room)
        self.hass.bus.async_fire(
            f"{DOMAIN}_room_done",
            {
                "vacuum": device.name, "duid": duid, "room": room, "reason": reason,
                "kind": self._kind_of(kinds), "kinds": list(kinds),
            },
        )

    def _track_and_emit(self, device: AnyVacDevice) -> None:
        """Fire the clean lifecycle events and drive the per-task accumulators.

        A TASK is one command the robot runs to its end (docs/52 §9.3): in a
        room-by-room job one room, in a clean started from the app or a batch
        dispatch all its rooms. The robot chains tasks without ever dropping
        ``in_cleaning`` (the next room is sent on its way home), so the end of
        a task is the edge into ``returning_home`` (not the firmware's low
        battery trip) or the fall of ``in_cleaning``, whichever comes first.

        * ``anyvac_clean_finished`` — one per task, with what it cleaned
          (``rooms_result``). The job runner reads its items' results from it.
        * ``anyvac_clean_started`` — once per run (the first task of a job).
        * ``anyvac_run_finished`` — without a job: when the robot is done (its
          ``in_cleaning`` falls). In a job the runner fires it per vacuum once
          its queue is done (docs/52 R6).

        Learning (calibration, completion %) happens at every task end: one
        task is one clean measurement, never a mix of two (docs/52 O3).
        """
        duid = device.duid
        cleaning = bool(device.data.get("in_cleaning"))
        was = self._was_cleaning.get(duid, False)
        state = device.data.get("status_state")
        prev_state = self._prev_state.get(duid)
        self._prev_state[duid] = state
        if cleaning and not was:
            # RUN vs SORTIE (docs/36): a later sortie of the same job (the robot
            # docked to wash, then got its next room) keeps the drawn trace;
            # a new job or a manual start wipes it.
            new_run = self._sortie_is_new_job(duid)
            self._path_seen[duid] = {"dry": 0, "wet": 0}
            run = self._runs.get(duid)
            if run is not None:
                run.break_path("dry")
                run.break_path("wet")
            self._task_last.pop(duid, None)
            if new_run:
                self._session_clean_type.pop(duid, None)
                self._signal_candidate.pop(duid, None)
                self._dry_path[duid] = []
                self._wet_path[duid] = []
                self._decim_cache.pop(duid, None)  # see `_decimate_segments` (1.1.0)
                # Persist the wipe right away (2026-07-26) — a poll without the
                # new session's first points must not leave the old trace on disk.
                self._delay_save(self._paths_store,
                    self._paths_for_save, _PATHS_WIPE_SAVE_DELAY_S
                )
                ct_start, ct_src = self.resolve_run_kind(duid)
                if ct_start is None:
                    ct_start = device.data.get("clean_type")
                    ct_src = "signal" if ct_start else None
                self.hass.bus.async_fire(
                    f"{DOMAIN}_clean_started",
                    {
                        "vacuum": device.name, "duid": duid,
                        "clean_type": ct_start, "clean_type_source": ct_src,
                    },
                )
                if self.hass is not None and getattr(self.hass, "async_create_task", None):
                    # The app's room order, for a multi-room clean started outside
                    # AnyVac (`_next_room`, docs/52 O1). Best-effort.
                    try:
                        self.hass.async_create_task(self.async_refresh_app_sequence(duid))
                    except Exception:  # noqa: BLE001 - test doubles without a loop
                        pass
            self._begin_task(device)
            self._dry_path_open[duid] = False
            self._wet_path_open[duid] = False
        elif cleaning and not self._task_open.get(duid) and state in ACTIVE_CLEAN_STATES:
            # The next task, accepted on the way home (docs/52 §2.8): no
            # `in_cleaning` edge, the robot just starts cleaning again.
            self._begin_task(device)
        if cleaning:
            room = device.data.get("vacuum_room_name")
            if room and self._task_open.get(duid):
                self._session_rooms.setdefault(duid, set()).add(room)
            self._capture_signal(device)
            battery = device.data.get("battery")
            if (
                self._task_open.get(duid)
                and state == "returning_home"
                and prev_state != "returning_home"
                and not (isinstance(battery, (int, float)) and battery <= LOW_BATTERY_RETURN_PCT)
            ):
                self._close_task(device)
        elif was:
            if self._task_open.get(duid):
                self._close_task(device)
            if duid not in self._job_rooms:
                event = self._task_last.pop(duid, None)
                if event is not None:
                    self.hass.bus.async_fire(f"{DOMAIN}_run_finished", event)
        self._was_cleaning[duid] = cleaning

    def _begin_task(self, device: AnyVacDevice) -> None:
        duid = device.duid
        if duid in self._harvest_pending:
            # The next room came before the next map: learn the last one now,
            # before its measurement is reset.
            self._harvest_now(device)
        self._task_open[duid] = True
        self._task_rooms_done[duid] = set()
        self._session_start[duid] = dt_util.utcnow()
        self._session_rooms[duid] = set()
        self._session_confirmed[duid] = set()
        self._room_elapsed[duid] = {}
        self._runs[duid] = covmod.RunTracker()
        self._run_targets_seen.pop(duid, None)

    def _close_task(self, device: AnyVacDevice) -> None:
        """End of one task: last seconds of cleaning, the last room's
        ``room_done``, the task's room results, learning, ``clean_finished``."""
        duid = device.duid
        # The last seconds of cleaning arrive with this very poll (the firmware
        # refreshes the map on the state change) — attribute them BEFORE the
        # harvest reads the run (docs/45 §1.2).
        self._attribute_points(device, tail_only=True)
        # The room the robot was in is finished now. Reported here because
        # `_detect_room_done` only sees the robot leave it once `in_cleaning`
        # falls, which a chained task never does.
        # A short room may never get confirmed (two snapshots): the room the
        # job sent counts too.
        for room in [self._confirmed_room.get(duid), *sorted(self._task_item.get(duid) or ())]:
            if room:
                self._emit_room_done(device, room, "task_end")
        self._confirmed_room[duid] = None
        self._raw_room[duid] = None
        self._raw_count[duid] = 0
        event = self._run_event(device)
        event["rooms_result"] = self._task_result(device)
        event["battery"] = device.data.get("battery")
        event["water_shortage"] = bool((device.data.get("dock_status") or {}).get("water_shortage_status"))
        self._task_open[duid] = False
        self._task_item.pop(duid, None)
        self.hass.bus.async_fire(f"{DOMAIN}_clean_finished", event)
        # Learning waits for the next map snapshot when the robot only turned
        # home: S7 MaxV's map with the room's last seconds came after the
        # state change (field 2026-11-25, Bathroom read 71 %). A robot that
        # already stopped (`in_cleaning` fell) is learned now.
        self._harvest_pending[duid] = {"at": dt_util.utcnow(), "polls": 0, "event": event}
        if not device.data.get("in_cleaning"):
            self._harvest_now(device)

    def _maybe_harvest_pending(self, device: AnyVacDevice) -> None:
        """Learn a closed task once a later map snapshot arrived, the robot
        stopped, or a minute passed — whichever is first."""
        pend = self._harvest_pending.get(device.duid)
        if pend is None:
            return
        pend["polls"] += 1
        age = (dt_util.utcnow() - pend["at"]).total_seconds()
        if (
            (pend["polls"] >= 2 and self._is_map_fresh(device.duid))
            or not device.data.get("in_cleaning")
            or age >= 60
        ):
            self._harvest_now(device)

    def _harvest_now(self, device: AnyVacDevice) -> None:
        duid = device.duid
        pend = self._harvest_pending.pop(duid, None) or {}
        event = pend.get("event") or self._run_event(device)
        calibrated = self._harvest_run(device)
        if calibrated:
            event["calibrated"] = calibrated
        if duid not in self._job_rooms:
            if device.data.get("in_cleaning"):
                self._task_last[duid] = event  # `run_finished` once it docks
            else:
                self.hass.bus.async_fire(f"{DOMAIN}_run_finished", event)

    def _task_result(self, device: AnyVacDevice) -> list[dict[str, Any]]:
        """What the task did per room (docs/52 §5b): read before the harvest
        resets the run. ``cleaned`` = really cleaned, not driven through."""
        duid = device.duid
        run = self._runs.get(duid) or covmod.RunTracker()
        geo = (self._geo.get(duid) or (None, None, None))[2]
        elapsed = self._room_elapsed.get(duid) or {}
        rooms = set(self._task_item.get(duid) or ()) | set(run.activated)
        out: list[dict[str, Any]] = []
        for nm in sorted(rooms):
            row: dict[str, Any] = {
                "room": nm,
                "cleaned": nm in run.activated,
                "active_min": round((elapsed.get(nm) or 0.0) / 60, 1),
                "passes": run.passes,
            }
            for kind in covmod.KINDS:
                res = run.kind_result(geo, nm, kind) if geo is not None else None
                if res is not None and res["floor"] >= 5:
                    row[kind] = 100 if res["pct"] >= 90 else res["pct"]
            out.append(row)
        return out

    # -- docs/49: what KIND of run was it? ----------------------------------------
    # One rule: EVIDENCE decides (the run's real dry/wet footprint); until evidence
    # exists the job's PLAN says what it ordered; the mode KNOB comes last, and only
    # the value captured during active cleaning (`_capture_signal`). The knob read at
    # the finish poll is never used — the S7 MaxV firmware resets the water mode in
    # the very state update that starts `returning_home`.

    @staticmethod
    def _kind_of(kinds: Any) -> str | None:
        """{"dry"} → "dry", {"wet"} → "wet", both → "both", nothing → None."""
        ks = {k for k in kinds if k in covmod.KINDS}
        if len(ks) == len(covmod.KINDS):
            return "both"
        return next(iter(ks)) if ks else None

    @staticmethod
    def _kinds_list(kind: str | None) -> list[str]:
        if kind == "both":
            return list(covmod.KINDS)
        return [kind] if kind in covmod.KINDS else []

    def _run_evidence(self, duid: str) -> dict[str, list[str]]:
        """{room: [kinds]} the running run has real footprint evidence for (lenient
        5 % floor, rooms the run actually cleaned — `RunTracker.activated`)."""
        run = self._runs.get(duid)
        if run is None:
            return {}
        geo = (self._geo.get(duid) or (None, None, None))[2]
        out: dict[str, list[str]] = {}
        for nm in run.activated:
            ks = [k for k in covmod.KINDS if run.floor_fraction(geo, nm, k) >= 0.05]
            if ks:
                out[nm] = ks
        return out

    def _plan_kinds(self, duid: str) -> set[str]:
        """Kinds the registered job ordered of this robot (open passes first)."""
        st = self._job_state
        if not st:
            return set()
        q = (st.get("queues") or {}).get(duid) or {}
        rows = [r for r in q.get("items") or [] if r.get("kind") in covmod.KINDS]
        cur = [r for r in rows if r.get("state") in ("sent", "active")]
        open_rows = cur or [r for r in rows if r.get("state") == "queued"]
        return {r["kind"] for r in (open_rows or rows)}

    def resolve_run_kind(self, duid: str) -> tuple[str | None, str | None]:
        """``(kind, source)`` of this vacuum's current run: kind ∈ dry | wet | both |
        None, source ∈ evidence | plan | signal | None."""
        kinds = {k for ks in self._run_evidence(duid).values() for k in ks}
        if kinds:
            return self._kind_of(kinds), "evidence"
        plan = self._plan_kinds(duid)
        if len(plan) == 1:
            return next(iter(plan)), "plan"
        sig = self._session_clean_type.get(duid)
        if sig in covmod.KINDS:
            return sig, "signal"
        return None, None

    def _capture_signal(self, device: AnyVacDevice) -> None:
        """Remember the mode knob — only during active, non-transit cleaning, the
        FIRST value that holds for two consecutive polls, independent of whether a
        room name is known. No "last value wins": the end-of-clean reset would win."""
        duid = device.duid
        if duid in self._session_clean_type:
            return
        d = device.data
        if (
            not d.get("in_cleaning")
            or d.get("transit")
            or d.get("status_state") not in ACTIVE_CLEAN_STATES
        ):
            self._signal_candidate.pop(duid, None)
            return
        sig = d.get("clean_type")
        if sig not in covmod.KINDS:
            return
        if self._signal_candidate.get(duid) == sig:
            self._session_clean_type[duid] = sig
            self._signal_candidate.pop(duid, None)
        else:
            self._signal_candidate[duid] = sig

    def _last_run_for_save(self) -> dict[str, Any]:
        return {duid: dict(rec) for duid, rec in self._last_run.items()}

    def _run_event(self, device: AnyVacDevice) -> dict[str, Any]:
        """The shared payload of ``clean_finished`` / ``run_finished``."""
        duid = device.duid
        started = self._session_start.get(duid)
        duration_min = round((dt_util.utcnow() - started).total_seconds() / 60) if started else None
        kind, source = self.resolve_run_kind(duid)
        return {
            "vacuum": device.name,
            "duid": duid,
            # docs/49: `both` is possible now (a combined pass leaves both footprints).
            "clean_type": kind,
            "clean_type_source": source,
            "kinds": self._kinds_list(kind),
            "rooms_kinds": self._run_evidence(duid),
            "rooms": sorted(self._session_rooms.get(duid, set())),
            "duration_min": duration_min,
        }

    def _harvest_run(self, device: AnyVacDevice) -> dict[str, dict[str, Any]]:
        """Close a finished RUN: calibrate, persist the per-room completion % (docs/45),
        clear the one-shot UI state and reset the per-run accumulators.

        Split out of `_track_and_emit` in docs/36 because it no longer runs on every
        ``in_cleaning`` falling edge — a job dispatched in batches docks between them,
        and harvesting each batch as if it were a whole clean is what made a room's %
        read 60 % after the first batch and then drag its baseline down to the size of
        that batch. Returns the ``calibrated`` payload (empty when nothing was learned).
        """
        duid = device.duid
        rooms = sorted(self._session_rooms.get(duid, set()))
        duration_min = self._run_event(device)["duration_min"]
        ct, ct_source = self.resolve_run_kind(duid)
        # Continuous calibration (docs/16 §4): EVERY room the run finished is a
        # sample — a single-room clean is just the trivial case. Which rooms were
        # really cleaned (not driven through) and how complete they are comes from
        # the run's `RunTracker` (docs/45); the point-weighted active time already
        # excludes pauses, transit and mop washes.
        # Which KIND(S) a room calibrates is decided by EVIDENCE (its dry/wet
        # footprint), never by the water-mode signal — an S7 dry pass once reported
        # clean_type=wet and poisoned the wet estimate table. Wet coverage only
        # exists while the mop is physically down; dry only while suction is on.
        # A combined pass rightfully learns the same minutes into BOTH tables.
        run = self._runs.get(duid) or covmod.RunTracker()
        geo = (self._geo.get(duid) or (None, None, None))[2]
        calibrated: dict[str, dict[str, Any]] = {}
        calib_rooms: dict[str, dict[str, Any]] = {}
        # Persistent per-room completion (docs/45 §2.4). Only rooms the run really
        # cleaned (`run.activated`) — a room merely driven through never gets a
        # number. A room the robot left for the next target is finished (100); the
        # room the run ended in reads its measured completion, snapped to 100 from
        # 90 up (the firmware ended the run inside it, the tail is measurement noise).
        completion: dict[str, dict[str, int]] = {}
        coverage_changed = False
        for nm in run.activated:
            for kind in covmod.KINDS:
                res = run.kind_result(geo, nm, kind)
                if res is None or res["floor"] < 5:
                    continue
                pct = 100 if res["pct"] >= 90 else res["pct"]
                completion.setdefault(nm, {})[kind] = pct
                rec = self._room_coverage.setdefault(nm, {})
                if rec.get(kind) != pct or rec.get(f"{kind}_floor") != res["floor"]:
                    rec[kind] = pct
                    rec[f"{kind}_floor"] = res["floor"]
                    coverage_changed = True
        for nm, sec in sorted((self._room_elapsed.get(duid) or {}).items()):
            active_min = round(sec / 60)
            room_rec: dict[str, Any] = {"active_min": active_min}
            for kind in covmod.KINDS:
                res = run.kind_result(geo, nm, kind)
                pct = (completion.get(nm) or {}).get(kind)
                krec: dict[str, Any] = {
                    "completion": pct,
                    "floor": res["floor"] if res else None,
                    "passes": run.passes,
                }
                if res is None or res["floor"] < 5:
                    krec["accepted"], krec["reason"] = False, "no evidence of this kind"
                elif nm not in run.activated:
                    krec["accepted"], krec["reason"] = False, "not cleaned (transit only)"
                elif pct is None or pct < 100:
                    krec["accepted"], krec["reason"] = False, f"completion {pct}% < 100%"
                elif not (1 <= active_min <= 180):
                    krec["accepted"], krec["reason"] = False, "active time out of 1-180 min range"
                else:
                    before, after = self._learn_estimate(duid, nm, kind, active_min)
                    if after is not None:
                        krec["accepted"], krec["reason"] = True, "ok"
                        krec["before"], krec["after"] = before, after
                        calibrated.setdefault(nm, {})[kind] = {"before": before, "after": after}
                    else:
                        krec["accepted"], krec["reason"] = False, "rejected by learner"
                room_rec[kind] = krec
            calib_rooms[nm] = room_rec
        if coverage_changed:
            self._delay_save(self._cov_pct_store, lambda: self._room_coverage, 5)
        self._record_reach(device, run, geo)
        self._last_calib[duid] = {
            "at": dt_util.utcnow().isoformat(timespec="seconds"),
            "clean_type": ct,
            "clean_type_source": ct_source,
            "duration_min": duration_min,
            "passes": run.passes,
            "cleaned_rooms": list(run.activated),
            "finished_rooms": list(run.done),
            "ignored_mm": {k: round(v) for k, v in run.ignored.items()},
            "accepted": sorted(calibrated),
            "rooms": calib_rooms,
        }
        # Auto-clear the finished rooms from the shared card-level selection —
        # this replaces the card's old client-side selection clearing (docs/14 §3.11;
        # room keys == integration room names by convention).
        if rooms and self._selected_rooms & set(rooms):
            self._selected_rooms -= set(rooms)
            self._delay_save(self._sel_store, lambda: sorted(self._selected_rooms), 2)
        # Pins are one-shot user overrides — clear them with the finished rooms
        # (docs/18 §7e), same lifecycle as the selection auto-clear above.
        # Per-kind since 2026-07-25: this session's evidence-based `ct` is a
        # single "dry" or "wet" (never both — one physical robot session is
        # one pass), so only the pass that actually just ran is cleared,
        # leaving the other pass's pin (e.g. a still-pending wet pin while
        # the dry robot finishes first in a "both" job) untouched. `ct`
        # unknown (no settings signal captured) falls back to clearing the
        # whole room, same as the old flat-pin behavior.
        if rooms and any(r in self._room_pins for r in rooms):
            pins_changed = False
            for r in rooms:
                kinds = self._room_pins.get(r)
                if not kinds:
                    continue
                if ct in ("dry", "wet", "both"):
                    for k in self._kinds_list(ct):
                        if kinds.pop(k, None) is not None:
                            pins_changed = True
                    if not kinds:
                        self._room_pins.pop(r, None)
                else:
                    self._room_pins.pop(r, None)
                    pins_changed = True
            if pins_changed:
                self._delay_save(self._pins_store, lambda: dict(self._room_pins), 2)
        self._session_rooms[duid] = set()
        # Clear the live per-room session accumulators NOW (calibration + baseline
        # learning above already consumed them). Without this, rooms_progress kept
        # showing residual transit percentages (e.g. "Kitchen 9 %" from the drive
        # home through it) until the NEXT session started — stale gauges on the card.
        self._room_elapsed[duid] = {}
        self._runs[duid] = covmod.RunTracker()
        self._run_targets_seen.pop(duid, None)
        self._session_confirmed[duid] = set()
        self._session_clean_type.pop(duid, None)
        self._signal_candidate.pop(duid, None)
        if ct is not None or rooms:
            self._last_run[duid] = {
                "kind": ct,
                "source": ct_source,
                "finished_at": dt_util.utcnow().isoformat(timespec="seconds"),
                "rooms": rooms,
            }
            self._delay_save(self._last_run_store, self._last_run_for_save, 2)
        return calibrated

    async def _async_setup(self) -> None:
        """Load persisted per-room clean history before the first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._history = {k: dict(v) for k, v in stored.items() if isinstance(v, dict)}
        sel = await self._sel_store.async_load()
        if isinstance(sel, list):
            self._selected_rooms = {str(r) for r in sel}
        sp_store = getattr(self, "_sp_store", None)  # absent in __new__-built test doubles
        self._service_points = _parse_service_points(await sp_store.async_load() if sp_store else None)
        pins = await self._pins_store.async_load()
        if isinstance(pins, dict):
            # Per-kind format (2026-07-25+): {room: {"dry"/"wet": vacuum}}. A
            # pre-migration flat {room: vacuum} entry carries no kind info to
            # migrate onto — dropped rather than guessed, equivalent to a
            # one-time reset to automatic assignment for any room pinned at
            # the moment of upgrade (consistent with the pin's existing
            # self-healing/one-shot lifecycle, docs/18 §7e).
            migrated: dict[str, dict[str, str]] = {}
            for k, v in pins.items():
                if not isinstance(v, dict):
                    continue
                kinds = {
                    kk: str(vv) for kk, vv in v.items() if kk in ("dry", "wet") and vv
                }
                if kinds:
                    migrated[str(k)] = kinds
            self._room_pins = migrated
        lay = await self._layers_store.async_load()
        if isinstance(lay, dict):
            self._view_layers = {
                "dry": bool(lay.get("dry", True)),
                "wet": bool(lay.get("wet", False)),
            }
        seats = await self._seats_store.async_load()
        if isinstance(seats, dict):
            loaded: dict[str, dict[str, Any]] = {}
            for src, entry in seats.items():
                if not isinstance(entry, dict):
                    continue
                vacs: dict[str, dict[str, Any]] = {}
                vacs_raw = entry.get("vacuums")
                if isinstance(vacs_raw, dict):
                    for ent, v in vacs_raw.items():
                        # BREAKING (integration 1.12.0, docs/42): the
                        # per-vacuum shape is now {"map": {...}, "appearance":
                        # {...}}, both independently optional. A stored entry
                        # that isn't a dict, or is still the pre-1.12.0 flat
                        # `{rotation,...}` shape (no "map"/"appearance" keys
                        # at all), is silently discarded — no migration, same
                        # defensive-load posture as every other malformed
                        # shape here.
                        if not isinstance(v, dict):
                            continue
                        vac_entry: dict[str, Any] = {}
                        m = v.get("map")
                        if isinstance(m, dict):
                            try:
                                rotation = float(m["rotation"])
                                scale = float(m["scale"])
                                offset_x = float(m["offset_x"])
                                offset_y = float(m["offset_y"])
                            except (KeyError, TypeError, ValueError):
                                rotation = scale = offset_x = offset_y = None  # type: ignore[assignment]
                            if (
                                rotation is not None
                                and math.isfinite(rotation)
                                and math.isfinite(offset_x)
                                and math.isfinite(offset_y)
                                and math.isfinite(scale)
                                and scale > 0
                            ):
                                parsed: dict[str, float] = {
                                    "rotation": rotation,
                                    "scale": scale,
                                    "offset_x": offset_x,
                                    "offset_y": offset_y,
                                }
                                scale_y_raw = m.get("scale_y")
                                if scale_y_raw is not None:
                                    try:
                                        scale_y = float(scale_y_raw)
                                    except (TypeError, ValueError):
                                        scale_y = None
                                    if scale_y is not None and math.isfinite(scale_y) and scale_y > 0:
                                        parsed["scale_y"] = scale_y
                                vac_entry["map"] = parsed
                        appearance_raw = v.get("appearance")
                        if isinstance(appearance_raw, dict):
                            parsed_appearance = _parse_stored_appearance(appearance_raw)
                            if parsed_appearance:
                                vac_entry["appearance"] = parsed_appearance
                        # docs/42 §4.4 (fáze K): "rooms" added alongside
                        # "map"/"appearance" above — same defensive-load
                        # posture, per-room_key re-validation via
                        # `_parse_stored_rooms`.
                        rooms_raw = v.get("rooms")
                        if isinstance(rooms_raw, dict):
                            parsed_rooms = _parse_stored_rooms(rooms_raw)
                            if parsed_rooms:
                                vac_entry["rooms"] = parsed_rooms
                        if vac_entry:
                            vacs[str(ent)] = vac_entry
                image_base = entry.get("image_base")
                if not isinstance(image_base, dict):
                    image_base = None
                # docs/42 §4.4 (fáze K): card-level "rooms" — sibling of
                # "image_base" on the entry itself, NOT nested under
                # "vacuums". Same defensive re-validation as the per-vacuum
                # one above.
                card_rooms_raw = entry.get("rooms")
                card_rooms = (
                    _parse_stored_rooms(card_rooms_raw)
                    if isinstance(card_rooms_raw, dict)
                    else {}
                )
                # docs/42 §3.3/§9 (fáze I addendum) — card-level "room_style",
                # same sibling-of-"image_base" posture as "rooms" above.
                room_style_raw = entry.get("room_style")
                room_style = (
                    _parse_stored_room_style(room_style_raw)
                    if isinstance(room_style_raw, dict)
                    else {}
                )
                if vacs or image_base or card_rooms or room_style:
                    updated = entry.get("updated")
                    loaded[str(src)] = {
                        "vacuums": vacs,
                        "image_base": image_base,
                        **({"rooms": card_rooms} if card_rooms else {}),
                        **({"room_style": room_style} if room_style else {}),
                        "updated": updated if isinstance(updated, str) else None,
                    }
            self._floorplan_seats = loaded
        # docs/45: the learned coverage baselines and the old coverage-% table
        # (different meaning — "like last time", not "work done") are gone; drop
        # their files once instead of leaving orphans in .storage.
        for legacy in (self._cov_store, self._cov_legacy_pct_store):
            try:
                await legacy.async_remove()
            except Exception as err:  # noqa: BLE001 - housekeeping only
                _LOGGER.debug("AnyVac: removing a legacy store failed: %s", err)
        last_run = await self._last_run_store.async_load()
        if isinstance(last_run, dict):
            self._last_run = {
                duid: dict(rec) for duid, rec in last_run.items() if isinstance(rec, dict)
            }
        reach_store = self.__dict__.get("_reach_store")  # absent in partial test coordinators
        reach_raw = await reach_store.async_load() if reach_store is not None else None
        if isinstance(reach_raw, dict):
            self._reach_masks = {
                duid: {nm: dict(rec) for nm, rec in rooms.items() if isinstance(rec, dict)}
                for duid, rooms in reach_raw.items()
                if isinstance(rooms, dict)
            }
        cov_pct = await self._cov_pct_store.async_load()
        if isinstance(cov_pct, dict):
            self._room_coverage = {
                room: {
                    k: int(v)
                    for k, v in kinds.items()
                    if k in ("dry", "wet", "dry_floor", "wet_floor") and isinstance(v, (int, float))
                }
                for room, kinds in cov_pct.items()
                if isinstance(kinds, dict)
            }
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
        paths = await self._paths_store.async_load()
        if isinstance(paths, dict):
            for raw_duid, rec in paths.items():
                if not isinstance(rec, dict):
                    continue
                duid = str(raw_duid)

                def _segs(val: Any) -> list[list[dict[str, float]]]:
                    if not isinstance(val, list):
                        return []
                    out: list[list[dict[str, float]]] = []
                    for seg in val:
                        if not isinstance(seg, list):
                            continue
                        out.append(
                            [
                                {"x": float(p["x"]), "y": float(p["y"])}
                                for p in seg
                                if isinstance(p, dict) and "x" in p and "y" in p
                            ]
                        )
                    return out

                dp = _segs(rec.get("dry_path"))
                if dp:
                    self._dry_path[duid] = dp
                wp = _segs(rec.get("wet_path"))
                if wp:
                    self._wet_path[duid] = wp
                ps = rec.get("path_seen")
                if isinstance(ps, dict):
                    self._path_seen[duid] = {
                        "dry": int(ps.get("dry", 0) or 0),
                        "wet": int(ps.get("wet", 0) or 0),
                    }
                if rec.get("dry_path_open"):
                    self._dry_path_open[duid] = True
                if rec.get("wet_path_open"):
                    self._wet_path_open[duid] = True
        hf_raw = await self._home_frame_store.async_load()
        if isinstance(hf_raw, dict):
            if homeframe.storage_is_current(hf_raw):
                self._home_frames, self._robot_frame = homeframe.frames_from_storage(hf_raw)
            elif hf_raw.get("frames"):
                _LOGGER.warning(
                    "AnyVac: home frames were built under older registration rules — "
                    "dropping %d frame(s) and rebuilding them from the vacuums' current "
                    "maps. A floorplan snapshotted from a home frame (crop_box.frame_id) "
                    "or calibrated against one (home_anchors) needs to be redone.",
                    len(hf_raw.get("frames") or {}),
                )

    def _history_for_save(self) -> dict[str, dict[str, str]]:
        return self._history

    def _home_frame_for_save(self) -> dict[str, Any]:
        """Snapshot for `self._home_frame_store.async_delay_save` (docs/40
        §4.2) — mirrors `_paths_for_save`'s pattern: a plain method (not a
        lambda inline at the call site) so the Fáze 1 registration pipeline
        (`coordinator integrace`, next) can debounce-save from more than one
        place without duplicating the snapshot logic."""
        return homeframe.frames_to_storage(self._home_frames, self._robot_frame)

    def _maybe_register_home_frame(self, device: AnyVacDevice) -> None:
        """Trigger docs/40 §4.2 home-frame registration for one duid — but
        only when its raw map actually changed since the last attempt (the
        §4.3 cache key `(map_index, map_sequence, hash(raw))`), never on
        every plain 30s poll otherwise. Cheap check only: the heavy FFT work
        itself always runs in the executor
        (`_async_run_home_frame_registration`), so this never blocks the
        polling coroutine it's called from."""
        duid = device.duid
        # 1.57.1: one registration at a time, for ALL robots. Concurrent runs
        # each worked on a snapshot taken before the others' results landed —
        # on a fresh start every robot saw no frames and founded its own, and
        # two robots joining one frame at once lost one merge. A robot skipped
        # here keeps its old cache key, so the next poll tries again.
        if duid in self._home_frame_disabled or self._home_frame_inflight:
            return
        dbg = device.data.get("debug_map")
        if not isinstance(dbg, dict):
            return
        raw_sha1 = dbg.get("raw_sha1")
        if raw_sha1 is None:
            return
        seen = self.__dict__.setdefault("_home_frame_raw_seen", {})
        if seen.get(duid) == raw_sha1:
            return
        fetched = self.raw_map_for(duid)
        if fetched is None:
            return
        raw, _meta = fetched
        # 1.57.1: the cache key is the decoded floor/wall grid, not the raw map.
        # The raw map (and its header's map_index counter) changes with every
        # upload — trace and position included — so 1.57.0 re-ran the FFT
        # registration on every map upload of a cleaning robot. Decoding the
        # grid costs ~1 ms; the registration only runs when the floor plan
        # itself changed.
        try:
            grid_sha1 = homeframe.grid_signature(homeframe.decode_grid(raw))
        except Exception:  # noqa: BLE001 - malformed raw: let the registration report it
            grid_sha1 = raw_sha1
        key = (dbg.get("map_index"), dbg.get("map_sequence"), grid_sha1)
        if (self._home_frame_cache_key.get(duid) or (None, None, None))[2] == grid_sha1:
            seen[duid] = raw_sha1
            return
        seen[duid] = raw_sha1
        lib_rooms = dict(device.data.get("_lib_rooms") or {})
        self._home_frame_inflight.add(duid)
        self.hass.async_create_task(
            self._async_run_home_frame_registration(duid, raw, key, lib_rooms)
        )

    async def _async_run_home_frame_registration(
        self, duid: str, raw: bytes, cache_key: tuple[Any, Any, str], lib_rooms: dict[int, Any]
    ) -> None:
        """Runs the pure, executor-safe `_compute_home_frame_result` off the
        event loop, then applies its result back on it (docs/40 §4.2/§4.3).
        Frames are only ever REPLACED wholesale — never mutated in place, see
        `homeframe.merge_robot_into_frame`/`grow_frame_canvas` — so this is
        safe even while another duid's registration is running concurrently
        against a snapshot of the same frame table."""
        try:
            frames_snapshot = dict(self._home_frames)
            robot_frame_snapshot = dict(self._robot_frame)
            now_iso = dt_util.utcnow().isoformat(timespec="seconds")
            result = await self.hass.async_add_executor_job(
                _compute_home_frame_result,
                frames_snapshot,
                robot_frame_snapshot,
                duid,
                raw,
                cache_key[0],
                cache_key[1],
                cache_key[2],
                lib_rooms,
                now_iso,
            )
        finally:
            self._home_frame_inflight.discard(duid)

        if result.get("disabled"):
            if duid not in self._home_frame_disabled:
                self._home_frame_disabled.add(duid)
                _LOGGER.warning(
                    "AnyVac: home-frame grid self-test failed for %s — disabling automatic "
                    "home-frame registration for this vacuum (%s). Its existing *_px "
                    "attributes are unaffected.",
                    duid, result.get("error"),
                )
            return
        if result.get("error"):
            _LOGGER.debug(
                "AnyVac: home-frame registration skipped for %s: %s", duid, result["error"]
            )
            return

        for fid, frame in result.get("updated_frames", {}).items():
            self._home_frames[fid] = frame
        for old_fid in result.get("removed_robot_from_frames", []):
            old_frame = self._home_frames.get(old_fid)
            if old_frame is not None and duid in (old_frame.get("robots") or {}):
                trimmed = dict(old_frame)
                trimmed["robots"] = {k: v for k, v in old_frame["robots"].items() if k != duid}
                if not trimmed["robots"]:
                    # 1.57.1: left empty (its robot joined a shared frame) —
                    # nothing references it as a live reference any more.
                    trimmed["stale"] = True
                self._home_frames[old_fid] = trimmed
            old_masks = dict(self._home_frame_room_masks.get(old_fid) or {})
            if duid in old_masks:
                old_masks.pop(duid)
                self._home_frame_room_masks[old_fid] = old_masks
        stale_fid = result.get("stale_frame_id")
        if stale_fid and stale_fid in self._home_frames and not self._home_frames[stale_fid].get("stale"):
            stale_frame = dict(self._home_frames[stale_fid])
            stale_frame["stale"] = True
            self._home_frames[stale_fid] = stale_frame
            _LOGGER.warning(
                "AnyVac: home-frame %s no longer matches vacuum %s's map (re-registration "
                "failed) — marked stale and a new frame was created for it. The old "
                "floorplan/crop_box tied to it will need to be re-accepted (docs/40 §4.2).",
                stale_fid, duid,
            )
        new_frame_id = result.get("robot_frame")
        if new_frame_id:
            self._robot_frame[duid] = new_frame_id
            frame_masks = dict(self._home_frame_room_masks.get(new_frame_id) or {})
            frame_masks[duid] = result.get("segment_masks") or {}
            self._home_frame_room_masks[new_frame_id] = frame_masks
        if result.get("origin_shifted"):
            _LOGGER.warning(
                "AnyVac: home-frame %s's origin shifted to accommodate vacuum %s's newly "
                "explored area (epoch bumped) — the card should re-read crop_box.",
                new_frame_id, duid,
            )
        self._home_frame_cache_key[duid] = result["cache_key"]
        self._delay_save(self._home_frame_store, self._home_frame_for_save, 5)
        self.async_update_listeners()

    def home_px_to_mm(self, duid: str, x: float, y: float) -> tuple[float, float] | None:
        """Home-frame px (docs/40 §4.1, `*_home_px` density) -> this vacuum's
        OWN mm space — the inverse of what `mm_to_home_px` publishes.
        Geometry stays owned by the coordinator (docs/14 §1); callers must
        not solve this transform themselves. None when this duid has no
        home-frame assignment yet (self-test disabled, no map seen since
        restart, or `unaligned` with no matching frame)."""
        frame_id = self._robot_frame.get(duid)
        frame = self._home_frames.get(frame_id) if frame_id else None
        if frame is None:
            return None
        rec = (frame.get("robots") or {}).get(duid)
        if rec is None:
            return None
        fx_mm, fy_mm = homeframe.home_px_to_mm(
            frame["origin_mm"],
            frame.get("cell_mm", homeframe.FRAME_CELL_MM),
            frame.get("scale", homeframe.HOME_PX_SCALE),
            x,
            y,
        )
        return homeframe.frame_mm_to_robot_mm(rec["rot_deg"], rec["tx_mm"], rec["ty_mm"], fx_mm, fy_mm)

    def mm_to_home_px(self, duid: str, x_mm: float, y_mm: float) -> tuple[float, float] | None:
        """Inverse of `home_px_to_mm` — this vacuum's own mm -> home-frame px."""
        frame_id = self._robot_frame.get(duid)
        frame = self._home_frames.get(frame_id) if frame_id else None
        if frame is None:
            return None
        rec = (frame.get("robots") or {}).get(duid)
        if rec is None:
            return None
        fx_mm, fy_mm = homeframe.robot_mm_to_frame_mm(rec["rot_deg"], rec["tx_mm"], rec["ty_mm"], x_mm, y_mm)
        return homeframe.mm_to_home_px(
            frame["origin_mm"],
            frame.get("cell_mm", homeframe.FRAME_CELL_MM),
            frame.get("scale", homeframe.HOME_PX_SCALE),
            fx_mm,
            fy_mm,
        )

    def home_frames_snapshot(self) -> dict[str, dict[str, Any]]:
        """Read-only view of every known home frame (docs/40 §4.1), keyed by
        `frame_id` — for services.py's Fáze 2 composite snapshot/export
        (`snapshot_map_as_floorplan`/`export_map_guide` with `frame: "home"`),
        which needs the frame's own `floor_mask`/`wall_mask` rasters directly
        (not per-robot geometry, so `home_px_to_mm`/`mm_to_home_px` don't fit).
        A shallow copy of the top-level dict — the individual frame dicts
        themselves are already copy-on-write everywhere they're built
        (never mutated in place), so handing out references is safe as long
        as the caller only reads them, which is all services.py does."""
        return dict(self._home_frames)

    def _paths_for_save(self) -> dict[str, dict[str, Any]]:
        """Snapshot of the segmented trace + the bookkeeping needed to resume
        diffing the robot's raw path buffers correctly next time (2026-07-26,
        see `_paths_store`) — one JSON-safe dict per known duid.

        Points are RDP-simplified before being written (1.1.0). The store is a
        full-file JSON rewrite on every save, and it used to persist the raw
        trajectory: measured at ~378 KiB per write for three vacuums at 3000
        points each, i.e. roughly 66 MiB rewritten over a three-hour whole-home
        job — enough to matter on the SD/eMMC installs this typically runs on.
        Simplification is lossless for our purposes: the card only ever receives
        an RDP-simplified path anyway (`_decimate_segments` on the way out), and
        the epsilon is below the robot's own positioning precision, so a restart
        restores a visually identical trace.

        Deliberately NOT simplified: `path_seen`, which counts RAW points
        consumed from the robot's buffers. That's a diff cursor, not geometry —
        rewriting it to the simplified count would make the next poll re-attribute
        points that were already counted.
        """
        duids = set(self._dry_path) | set(self._wet_path) | set(self._path_seen)

        def _thin(segments: list[list[dict[str, float]]]) -> list[list[dict[str, float]]]:
            return [_rdp_simplify(seg, _RDP_EPSILON_MM) for seg in segments]

        return {
            duid: {
                "dry_path": _thin(self._dry_path.get(duid, [])),
                "wet_path": _thin(self._wet_path.get(duid, [])),
                "path_seen": self._path_seen.get(duid, {"dry": 0, "wet": 0}),
                "dry_path_open": self._dry_path_open.get(duid, False),
                "wet_path_open": self._wet_path_open.get(duid, False),
            }
            for duid in duids
        }

    def _evidence_kinds(self, device: AnyVacDevice, room: str, strict: bool = True) -> list[str]:
        """Which clean kinds have real EVIDENCE in `room` this run (docs/16 → docs/45).

        Ground truth is the run's footprint coverage: wet only exists while the mop
        is physically engaged, dry only while suction is on, and only for a room
        the run actually cleaned (`RunTracker.activated` — a room driven through
        never qualifies, so it can neither be stamped "cleaned" nor release the
        wet robot). A kind counts from 30 % of the room's reachable floor
        (``strict``) or 5 % (``strict=False``, rooms the firmware itself lists as
        cleaned)."""
        run = self._runs.get(device.duid)
        if run is None or room not in run.activated:
            return []
        geo = (self._geo.get(device.duid) or (None, None, None))[2]
        floor = 0.3 if strict else 0.05
        return [k for k in covmod.KINDS if run.floor_fraction(geo, room, k) >= floor]

    def _stamp_room(self, device: AnyVacDevice, name: str | None, strict: bool = True) -> None:
        """Record that `name` was cleaned now — kinds decided by evidence (see
        _evidence_kinds). Keyed by room NAME so history aggregates across all vacuums.
        Persisted across restarts."""
        if not name:
            return
        kinds = self._evidence_kinds(device, name, strict)
        if not kinds:
            return
        now = dt_util.utcnow().isoformat()
        rec = self._history.setdefault(name, {})
        rec["any"] = now
        for kind in kinds:
            rec[kind] = now
        self._delay_save(self._store, self._history_for_save, 5)

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
        for seg in device.data.get("cleaned_rooms") or []:
            # Firmware-confirmed cleans use the lenient evidence floor.
            self._stamp_room(device, seg_to_name.get(str(seg)), strict=False)

    def _build_progress(self, device: AnyVacDevice) -> dict[str, dict[str, Any]]:
        """Live per-room completion of the running run (docs/45):
        {room: {dry_pct, wet_pct, spatial_pct, dry_floor, wet_floor, passes,
        dry_pass, wet_pass, active, done, elapsed_s, est_s, time_pct}}.

        ``*_pct`` = % of the ORDERED work done (passes included), ``*_floor`` =
        % of the room's reachable floor the footprint covered, ``*_pass`` = which
        pass the robot is on. Only rooms the run really cleaned appear — a room
        merely driven through gets no entry at all. ``*_calibrating`` stays in
        the payload as False for cards older than 1.45.0."""
        duid = device.duid
        run = self._runs.get(duid)
        if run is None:
            return {}
        geo = (self._geo.get(duid) or (None, None, None))[2]
        elapsed = self._room_elapsed.get(duid, {})
        ests = self._estimates.get(duid, {})
        ctype = self.resolve_run_kind(duid)[0]
        out: dict[str, dict[str, Any]] = {}
        for nm in run.activated:
            res = {k: run.kind_result(geo, nm, k) for k in covmod.KINDS}
            pcts = [r["pct"] for r in res.values() if r]
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
                "spatial_pct": max(pcts) if pcts else None,
                "dry_pct": res["dry"]["pct"] if res["dry"] else None,
                "wet_pct": res["wet"]["pct"] if res["wet"] else None,
                "dry_floor": res["dry"]["floor"] if res["dry"] else None,
                "wet_floor": res["wet"]["floor"] if res["wet"] else None,
                "dry_pass": res["dry"]["pass"] if res["dry"] else None,
                "wet_pass": res["wet"]["pass"] if res["wet"] else None,
                "passes": run.passes,
                "active": nm == run.active,
                "done": nm in run.done,
                "dry_calibrating": False,
                "wet_calibrating": False,
                "elapsed_s": round(el) if el is not None else None,
                "est_s": est_s,
                "time_pct": time_pct,
            }
        return out

    @callback
    def _ct_trace_on_event(self, event: Any) -> None:
        """Remember which clean_type AnyVac's own clean events carried (A0)."""
        duid = event.data.get("duid")
        if not duid:
            return
        events = self._ct_trace_events.setdefault(duid, deque(maxlen=_CT_TRACE_EVENTS))
        events.append(
            {
                "event": event.event_type.removeprefix(f"{DOMAIN}_"),
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "clean_type": event.data.get("clean_type"),
            }
        )

    def _record_clean_type_trace(
        self, device: AnyVacDevice, session_before: str | None
    ) -> None:
        """Keep the last full polls of a clean and freeze them a few polls after
        its falling edge into `clean_type_trace` (A0).

        Called once per full poll AFTER the pipeline, so ``signal`` is the
        clean_type `_extract_device` derived for this poll and ``session_after`` is
        what `_track_and_emit` kept. ``event_clean_type`` on the edge poll is the
        value `_run_event` built the finish events from (session first, then this
        poll's signal) — the field report's symptom in one number."""
        duid = device.duid
        cleaning = bool(device.data.get("in_cleaning"))
        prev = self._ct_trace_prev.get(duid, False)
        self._ct_trace_prev[duid] = cleaning
        tail = self._ct_trace_tail.get(duid)
        if not cleaning and not prev and tail is None:
            return
        buf = self._ct_trace_buf.setdefault(
            duid, deque(maxlen=_CT_TRACE_BEFORE + 1 + _CT_TRACE_AFTER)
        )
        edge = prev and not cleaning
        sig = device.data.get("mop_signal") or {}
        signal = device.data.get("clean_type")
        buf.append(
            {
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "edge": edge,
                "in_cleaning": cleaning,
                "status_state": device.data.get("status_state"),
                "transit": bool(device.data.get("transit")),
                "vacuuming": bool(device.data.get("vacuuming")),
                "room": device.data.get("vacuum_room_name"),
                "water_mode_name": sig.get("water_mode_name"),
                "water_box_carriage_status": sig.get("water_box_carriage_status"),
                "fan_speed_name": sig.get("fan_speed_name"),
                "signal": signal,
                "session_before": session_before,
                "session_after": self._session_clean_type.get(duid),
                "event_clean_type": (session_before or signal) if edge else None,
                "task_open": bool(self._task_open.get(duid)),
                "dry_points": sum(len(seg) for seg in self._dry_path.get(duid, [])),
                "wet_points": sum(len(seg) for seg in self._wet_path.get(duid, [])),
            }
        )
        if edge:
            tail = _CT_TRACE_AFTER
        elif tail is not None:
            tail -= 1
        if tail is not None and tail <= 0:
            edges = [p["at"] for p in buf if p["edge"]]
            self._ct_trace_last[duid] = {
                "edge_at": edges[-1] if edges else None,
                "polls": list(buf),
            }
            buf.clear()
            tail = None
        if tail is None:
            self._ct_trace_tail.pop(duid, None)
        else:
            self._ct_trace_tail[duid] = tail

    def _clean_type_trace(self, duid: str) -> dict[str, Any] | None:
        """The published `clean_type_trace` attribute (A0)."""
        last = self._ct_trace_last.get(duid)
        events = self._ct_trace_events.get(duid)
        if last is None and not events:
            return None
        return {**(last or {}), "events": list(events or [])}

    def _coverage_debug(self, device: AnyVacDevice) -> dict[str, Any] | None:
        """What the room-completion tracker currently believes (docs/45) — for the
        card's debug view and field reports."""
        run = self._runs.get(device.duid)
        cached = self._geo.get(device.duid)
        if run is None and cached is None:
            return None
        return {
            "geometry": (cached[1][0] if cached else None),
            "targets": sorted(self._run_targets_seen.get(device.duid) or ()) or None,
            "active": run.active if run else None,
            "finished": list(run.done) if run else [],
            "passes": run.passes if run else None,
            "ignored_mm": {k: round(v) for k, v in (run.ignored if run else {}).items()} or None,
        }

    async def _async_update_data(self) -> dict[str, AnyVacDevice]:
        """Read every Roborock v1 coordinator and normalise its map data."""
        result: dict[str, AnyVacDevice] = {}
        all_coords: list[Any] = []
        for rb_entry in self.hass.config_entries.async_entries(ROBOROCK_DOMAIN):
            runtime = getattr(rb_entry, "runtime_data", None)
            coords = getattr(runtime, "v1", None) or []
            all_coords.extend(coords)
            for coord in coords:
                try:
                    device = self._extract_device(coord)
                except Exception as err:  # noqa: BLE001 - one bad device must not fail the rest
                    _LOGGER.debug("AnyVac: failed reading a Roborock coordinator: %s", err)
                    continue
                if device is not None:
                    self._note_snapshot(device.duid, _raw_map_bytes(coord))
                    if self._is_map_fresh(device.duid):
                        # docs/48 §1.3: the new snapshot already contains every
                        # point the live trail added — drop it.
                        getattr(self, "_live", {}).pop(device.duid, None)
                        getattr(self, "_live_pub", {}).pop(device.duid, None)
                        st = getattr(self, "_live_stats", {}).get(device.duid)
                        if st is not None:
                            st["snapshot_at"] = dt_util.utcnow().isoformat(timespec="seconds")
                            st["since_snapshot"] = {"with_points": 0, "empty": 0, "no_diff": 0, "no_answer": 0, "failed": 0}
                    # docs/40 §4.2/§4.3: cheap cache-key check inline, heavy
                    # FFT registration itself always in the executor — never
                    # blocks this polling coroutine.
                    self._maybe_register_home_frame(device)
                    dbg = device.data.get("debug_map")
                    if isinstance(dbg, dict):
                        seen = self._debug_seen.setdefault(device.duid, {})
                        for pname in ("goto_path", "predicted_path", "goto"):
                            val = dbg.get(pname)
                            n = dbg.get(f"{pname}_points", 1 if val else 0)
                            if val and n:
                                seen[pname] = {
                                    "points": n,
                                    "at": dt_util.utcnow().isoformat(timespec="seconds"),
                                    "status_state": device.data.get("status_state"),
                                    "sample": val[:20] if isinstance(val, list) else val,
                                }
                        dbg["watermarks"] = seen or None
                    ct_session_before = self._session_clean_type.get(device.duid)
                    self._update_history(device)
                    self._detect_room_done(device)
                    self._track_and_emit(device)
                    self._attribute_points(device)
                    self._maybe_harvest_pending(device)
                    self._record_clean_type_trace(device, ct_session_before)
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
                        cov_rec = self._room_coverage.get(room.get("name")) or {}
                        room["coverage_dry"] = cov_rec.get("dry")
                        room["coverage_wet"] = cov_rec.get("wet")
                    device.data["rooms_last_cleaned"] = {k: dict(v) for k, v in self._history.items()}
                    device.data["rooms_estimate"] = {
                        r: dict(c) for r, c in (self._estimates.get(device.duid) or {}).items()
                    }
                    device.data["rooms_progress"] = progress
                    device.data["rooms_coverage"] = {k: dict(v) for k, v in self._room_coverage.items()}
                    self._reach_publish(device)
                    device.data.pop("_path_dry", None)
                    device.data.pop("_path_wet", None)
                    device.data.pop("_lib_rooms", None)
                    # Typed trace layers (docs/14 §3.9): path_dry = trajectory segmented
                    # to actual cleaning (no transit / mop-wash driving); path_wet = the
                    # mop trace. ``path`` (full trajectory) stays for backward compat.
                    # Both are LISTS OF SEGMENTS — each a contiguous run of points with no
                    # excluded gap inside it — so the card draws one polyline per segment
                    # instead of bridging gaps with a straight line (dry: bug found
                    # 2026-07-15, docs/14 §3.9; wet: cross-sortie stitching, docs/27 —
                    # a dock trip between two dispatches of the same orchestrated job must
                    # not draw as a straight line either).
                    dcache = self._decim_cache.setdefault(device.duid, {})
                    dry_segs = self._dry_path.get(device.duid, [])
                    path_dry = _decimate_segments(
                        dry_segs, PATH_MAX_POINTS, dcache.setdefault("dry", {})
                    )
                    device.data["path_dry_points"] = sum(len(s) for s in dry_segs)
                    wet_segs = self._wet_path.get(device.duid, [])
                    path_wet = _decimate_segments(
                        wet_segs, PATH_MAX_POINTS, dcache.setdefault("wet", {})
                    )
                    device.data["path_wet_points"] = sum(len(s) for s in wet_segs)
                    device.data["duid"] = device.duid
                    device.data["calib_debug"] = self._last_calib.get(device.duid)
                    # Room-completion tracker state (docs/45): run targets, active and
                    # finished rooms, passes, path driven through rooms that were NOT
                    # counted. Replaces the docs/17 `transit_cells` debug counter.
                    device.data["coverage_debug"] = self._coverage_debug(device)
                    device.data["clean_type_trace"] = self._clean_type_trace(device.duid)
                    # docs/49: `clean_type` is the verdict about the RUN (null when
                    # idle); the knob itself — the old meaning — moves to `mode_signal`.
                    device.data["mode_signal"] = device.data.get("clean_type")
                    if device.data.get("in_cleaning") or self._task_open.get(device.duid):
                        kind, kind_src = self.resolve_run_kind(device.duid)
                    else:
                        kind = kind_src = None
                    device.data["clean_type"] = kind
                    device.data["clean_type_source"] = kind_src
                    device.data["last_run"] = self._last_run.get(device.duid)
                    # Kontrakt v2 (docs/14 §3.6 + §5): geometry in rendered image
                    # PIXELS so the card never has to do mm math again.
                    aff = _solve_affine(device.data.get("calibration_points"))
                    device.data["schema_version"] = 3
                    if aff is not None:
                        device.data["vacuum_position_px"] = _px_point(
                            device.data.get("vacuum_position"), aff
                        )
                        device.data["charger_px"] = _px_point(device.data.get("charger"), aff)
                        # Segment-preserving px conversion: one sub-list per contiguous
                        # run (drop segments that yield zero valid px points). Both dry
                        # and wet are segmented (docs/27) — same shape, same conversion.
                        device.data["path_dry_px"] = [
                            seg_px
                            for seg in path_dry
                            if (seg_px := [q for p in seg if (q := _px_point(p, aff))])
                        ]
                        device.data["path_wet_px"] = [
                            seg_px
                            for seg in path_wet
                            if (seg_px := [q for p in seg if (q := _px_point(p, aff))])
                        ]
                        for room in device.data.get("rooms", []):
                            p0 = _px_point({"x": room.get("x0"), "y": room.get("y0")}, aff)
                            p1 = _px_point({"x": room.get("x1"), "y": room.get("y1")}, aff)
                            if p0 and p1:
                                # Normalise after the transform — the y axis flips.
                                room["bbox_px"] = {
                                    "x0": min(p0["x"], p1["x"]),
                                    "y0": min(p0["y"], p1["y"]),
                                    "x1": max(p0["x"], p1["x"]),
                                    "y1": max(p0["y"], p1["y"]),
                                }
                            else:
                                room["bbox_px"] = None
                    else:
                        device.data["vacuum_position_px"] = None
                        device.data["charger_px"] = None
                        device.data["path_dry_px"] = []
                        device.data["path_wet_px"] = []
                        for room in device.data.get("rooms", []):
                            room["bbox_px"] = None
                    _apply_home_frame_contract(
                        device, path_dry, path_wet,
                        self._home_frames, self._robot_frame, self._home_frame_room_masks,
                    )
                    device.data["service_points"] = self._published_service_points(device, aff)
                    # Legacy mm-space attributes (1.1.0). The card has not read
                    # any of these since Fáze 3 of the canon (docs/14) moved it
                    # onto the px contract — verified by scanning every attribute
                    # the card and editor actually touch. They are pure websocket
                    # payload: four point arrays of up to PATH_MAX_POINTS each,
                    # measured at ~224 KiB per vacuum per poll (~670 KiB every
                    # 30 s for a three-vacuum fleet), pushed to every open tab.
                    #
                    # Not deleted outright, because anyvac/README.md documents
                    # them as available for the user's own automations — so they
                    # stay behind an opt-in config-entry option (default off).
                    #
                    # Only the four big ARRAYS are dropped. The single-point mm
                    # fields (`vacuum_position`, `charger`) and especially
                    # `calibration_points` stay unconditionally: they cost a few
                    # points each, and `pct_to_mm()` — the pct->mm conversion
                    # behind Pin & Go and zone clean — reads `calibration_points`
                    # back off `self.data` on every call. `path_points` /
                    # `mop_path_points` stay too (two integers, read by the
                    # editor's debug rows and by the sensor's own state).
                    if self._expose_legacy_mm:
                        device.data["path_dry"] = path_dry
                        device.data["path_wet"] = path_wet
                    else:
                        for key in ("path", "mop_path", "path_dry", "path_wet"):
                            device.data.pop(key, None)
                    result[device.duid] = device

        # Observability (docs/13 B6): a vacuum we used to read suddenly yields no map
        # data -> one clear warning instead of silent degradation.
        missing = self._known_duids - set(result)
        if missing and not self._pipeline_warned:
            _LOGGER.warning(
                "AnyVac: no map data for previously seen vacuum(s) %s — the Roborock "
                "integration may have changed internally, be reloading, or the map is "
                "unavailable. AnyVac sensors for them will be unavailable until data returns.",
                sorted(missing),
            )
            self._pipeline_warned = True
        if not missing and self._pipeline_warned:
            _LOGGER.info("AnyVac: map data restored for all known vacuums.")
            self._pipeline_warned = False
        self._known_duids |= set(result)
        # Kontrakt v2 (docs/14 §5, docs/13 B6): the piggyback must not die silently —
        # expose pipeline health on every sensor so the card can show a warning.
        err_txt = (
            "no map data for previously seen vacuum(s): " + ", ".join(sorted(missing))
            if missing
            else None
        )
        for device in result.values():
            device.data["pipeline_ok"] = not missing
            device.data["pipeline_error"] = err_txt
        self._sync_roborock_listeners(all_coords)
        return result

    # ------------------------------------------------------------------
    # Map-snapshot driven refresh (docs/47)
    # ------------------------------------------------------------------

    def _is_map_fresh(self, duid: str) -> bool:
        """Did this poll see a new map snapshot for `duid` (docs/47 §1.3)?

        True when unknown — no raw bytes available (older library) or a caller
        that never went through `_note_snapshot` (unit tests driving the
        pipeline methods directly): that is exactly the pre-docs/47 behaviour.
        """
        return getattr(self, "_map_fresh", {}).get(duid, True)

    def _note_snapshot(self, duid: str, raw: bytes | None) -> None:
        """Record the raw map this poll is about to process and whether it is
        a new snapshot. Equality, not identity: python-roborock < 7.12 builds a
        new bytes object on every refresh even when nothing changed."""
        if raw is None:
            self._map_fresh[duid] = True
            return
        prev = self._rb_seen_raw.get(duid)
        self._map_fresh[duid] = not (prev is raw or prev == raw)
        self._rb_seen_raw[duid] = raw

    def _sync_roborock_listeners(self, coords: list[Any]) -> None:
        """Keep exactly one listener on each live Roborock v1 coordinator.

        A Roborock reload replaces its coordinator objects — new ones get a
        listener on our next poll, vanished ones are unsubscribed here.
        """
        live: set[int] = set()
        for coord in coords:
            key = id(coord)
            live.add(key)
            if key in self._rb_listeners:
                continue
            add = getattr(coord, "async_add_listener", None)
            if not callable(add):
                continue
            try:
                unsub = add(lambda c=coord: self._on_roborock_update(c))
            except Exception as err:  # noqa: BLE001 - never break the poll over this
                _LOGGER.debug("AnyVac: could not subscribe to a Roborock coordinator: %s", err)
                continue
            self._rb_listeners[key] = (coord, unsub)
        for key in [k for k in self._rb_listeners if k not in live]:
            _coord, unsub = self._rb_listeners.pop(key)
            unsub()

    @callback
    def _on_roborock_update(self, coord: Any) -> None:
        """A Roborock coordinator published new data (docs/47 §1.1).

        Refresh only when its map actually changed — a status push alone is
        ignored on purpose: it arrives before the new map, and a falling edge
        processed against the old map loses the last room's final points
        (docs/45 §1.2). Roborock fetches the map on every state change, so the
        state change reaches us together with that map.
        """
        duid = getattr(coord, "duid", None)
        raw = _raw_map_bytes(coord)
        if duid is None or raw is None:
            return
        prev = self._rb_seen_raw.get(duid)
        if prev is raw or prev == raw:
            return
        self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------------
    # Live position from the local diff (docs/48)
    # ------------------------------------------------------------------

    def live_for(self, duid: str) -> dict[str, Any] | None:
        """The `live` attribute of one vacuum's sensor (docs/48 §1.5)."""
        return self._live_pub.get(duid)

    def live_stats_for(self, duid: str) -> dict[str, Any] | None:
        """The `live_stats` attribute (docs/48 follow-up diagnostics).

        A COPY: the counters are mutated in place, and HA keeps the previous
        state's attributes by reference — handing out the live dict made the
        new state compare equal to the old one, so the frontend never saw the
        counters move (field-caught 2026-10-08, 1.51.1)."""
        st = getattr(self, "_live_stats", {}).get(duid)
        if st is None:
            return None
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}

    def _live_count(self, duid: str, what: str, **extra: Any) -> None:
        stats = getattr(self, "_live_stats", None)
        if stats is None:
            return
        zero = {"with_points": 0, "empty": 0, "no_diff": 0, "no_answer": 0, "failed": 0}
        st = stats.setdefault(duid, {**zero, "since_snapshot": dict(zero)})
        st[what] = st.get(what, 0) + 1
        st["since_snapshot"][what] = st["since_snapshot"].get(what, 0) + 1
        st["last"] = {"what": what, "at": dt_util.utcnow().isoformat(timespec="seconds"), **extra}

    def start_live(self) -> CALLBACK_TYPE:
        """Start the live poller; returns its unsubscribe callback."""
        if not self._live_enabled:
            return lambda: None
        return async_track_time_interval(
            self.hass, self._live_tick, timedelta(seconds=LIVE_DIFF_INTERVAL_S)
        )

    @callback
    def _live_tick(self, _now: Any = None) -> None:
        now = time.monotonic()
        for duid, device in (self.data or {}).items():
            if not device.data.get("in_cleaning") or duid in self._live_inflight:
                continue
            if self._live_backoff.get(duid, 0.0) > now:
                continue
            if duid in getattr(self, "_live_unsupported", ()):
                continue
            self._live_inflight.add(duid)
            self.hass.async_create_background_task(
                self._live_fetch(duid), f"anyvac live diff {duid}"
            )

    def _live_failed(self, duid: str) -> None:
        n = self._live_fails.get(duid, 0) + 1
        self._live_fails[duid] = n
        if n >= LIVE_DIFF_MAX_FAILS:
            self._live_fails[duid] = 0
            self._live_backoff[duid] = time.monotonic() + LIVE_DIFF_BACKOFF_S
            _LOGGER.debug("AnyVac: live diff for %s paused for %ss", duid, LIVE_DIFF_BACKOFF_S)

    def _mark_live_unsupported(self, duid: str, reason: str) -> None:
        """Stop the live poller for one robot until HA restarts (docs/48 §4.2).

        `reason`: "method" = the firmware rejects get_dynamic_map_diff (S6),
        "no_diff" = it answers but never with a diff (S7 MaxV)."""
        self._live_unsupported.add(duid)
        st = getattr(self, "_live_stats", {}).get(duid)
        if st is not None:
            st["unsupported"] = reason
        _LOGGER.info(
            "AnyVac: %s gives no get_dynamic_map_diff data (%s) — live position "
            "off for it, the map updates with the official integration (~30 s)",
            duid, reason,
        )
        self.async_update_listeners()

    async def _live_fetch(self, duid: str) -> None:
        try:
            rb = self.roborock_coordinator_for(duid)
            if rb is None:
                return
            v1ch = localchannel.v1_channel_of(rb)
            if not getattr(v1ch, "is_local_connected", False):
                return  # not a failure: no local connection right now
            res = await localchannel.local_request(
                v1ch, "get_dynamic_map_diff",
                timeout_s=LIVE_DIFF_TIMEOUT_S, watch_cloud=False,
            )
            parsed = livediff.parse_diff(res.get("ack"))
            if parsed is None:
                err = res.get("ack_error") or ""
                self._live_count(duid, "no_answer", ack=repr(res.get("ack"))[:80], ack_error=err)
                if "not recognized" in err.lower() or "unknown method" in err.lower():
                    self._mark_live_unsupported(duid, "method")
                    return
                self._live_failed(duid)
                return
            self._live_fails[duid] = 0
            diff = (res.get("ack") or {}).get("diff")
            if not isinstance(diff, dict):
                # `{"nonce": 0, "result": 2}` — a valid answer that never
                # carries a diff. S7 MaxV answers only this, even while
                # cleaning (field 2026-10-08): its firmware keeps no diff.
                # After LIVE_DIFF_NODIFF_LIMIT such answers in a row the
                # poller stops for that robot (same as an unsupported method).
                n = self._live_nodiff.get(duid, 0) + 1  # reset by any real diff
                self._live_nodiff[duid] = n
                self._live_count(duid, "no_diff", ack=repr(res.get("ack"))[:80],
                                 ms=res.get("latency_ms"))
                if n >= LIVE_DIFF_NODIFF_LIMIT:
                    self._mark_live_unsupported(duid, "no_diff")
                return
            self._live_nodiff[duid] = 0
            self._live_count(
                duid, "with_points" if parsed["points"] else "empty",
                start=parsed["start"], points=len(parsed["points"]),
                max_len=(diff.get("3") or {}).get("max_len"),
                c26=(diff.get("26") or {}).get("count"),
                ms=res.get("latency_ms"),
            )
            self._apply_live(duid, parsed)
        except Exception as err:  # noqa: BLE001 - display-only, never fatal
            _LOGGER.debug("AnyVac: live diff for %s failed: %s", duid, err)
            self._live_count(duid, "failed", error=f"{type(err).__name__}: {err}"[:120])
            self._live_failed(duid)
        finally:
            self._live_inflight.discard(duid)

    def _apply_live(self, duid: str, parsed: dict[str, Any]) -> None:
        """Merge one parsed diff into the live trail and publish (docs/48 §1.3)."""
        device = (self.data or {}).get(duid)
        if device is None:
            return
        base = int(device.data.get("path_points") or 0)
        trail = self._live.get(duid)
        if trail is None or trail.base != base:
            trail = self._live[duid] = livediff.LiveTrail(base=base)
        if not trail.apply(parsed):
            # An answer that moved nothing changes only the `live_stats` counters.
            # Pushing those every 3 s is a `state_changed` per answer and with it a
            # recorder row (47 rows in 3.5 min measured 2026-11-10, all with the
            # same stored attributes — docs/49 D1). They ride along with the next
            # real update or the regular poll instead.
            return
        self._live_pub[duid] = self._live_payload(device, trail)
        self.async_update_listeners()

    def _live_payload(self, device: AnyVacDevice, trail: livediff.LiveTrail) -> dict[str, Any]:
        """Trail → px-space `live` attribute (docs/48 §1.4–1.5). Same
        transforms as the snapshot's own `*_px` / `*_home_px` (docs/14)."""
        duid = device.duid
        dry_open = self._dry_path_open.get(duid, False)
        wet_open = self._wet_path_open.get(duid, False)
        dry = [trail.dry_segment()] if dry_open and trail.points else []
        wet = trail.wet_segments()
        aff = _solve_affine(device.data.get("calibration_points"))

        def _px(segs: list[list[dict[str, float]]]) -> list[list[dict[str, float]]]:
            if aff is None:
                return []
            return [s for seg in segs if (s := [q for p in seg if (q := _px_point(p, aff))])]

        frame_id = self._robot_frame.get(duid)
        frame = self._home_frames.get(frame_id) if frame_id else None
        rec = (frame.get("robots") or {}).get(duid) if frame is not None else None

        def _hpx(segs: list[list[dict[str, float]]]) -> list[list[dict[str, float]]]:
            if rec is None:
                return []
            return [
                s for seg in segs if (s := [q for p in seg if (q := _home_px_point(p, rec, frame))])
            ]

        return {
            "base_points": trail.base,
            "seq": trail.seq,
            "at": dt_util.utcnow().isoformat(timespec="seconds"),
            "broken": trail.broken,
            "pos_px": _px_point(trail.pos, aff) if aff is not None else None,
            "pos_home_px": _home_px_point(trail.pos, rec, frame),
            "dry_px": _px(dry),
            "wet_px": _px(wet),
            "dry_home_px": _hpx(dry),
            "wet_home_px": _hpx(wet),
            "dry_continues": bool(dry),
            "wet_continues": wet_open and trail.wet_starts_on_first_point(),
        }

    def unsubscribe_roborock(self) -> None:
        """Drop every Roborock listener (entry unload)."""
        for _coord, unsub in self._rb_listeners.values():
            unsub()
        self._rb_listeners.clear()

    async def async_shutdown(self) -> None:
        """Unsubscribe from Roborock before the base shutdown."""
        self.unsubscribe_roborock()
        for unsub in self._ct_trace_unsub:
            unsub()
        self._ct_trace_unsub = []
        await super().async_shutdown()
        await self._flush_stores()

    def _delay_save(self, store: Store, data_func: Callable[[], Any], delay: float) -> None:
        """`store.async_delay_save` that `_flush_stores` can complete on unload."""
        self._pending_saves[store] = data_func
        store.async_delay_save(data_func, delay)

    async def _flush_stores(self) -> None:
        """Write every Store with a delayed save now (B2).

        `Store.async_save` cancels the store's pending delayed write, so the
        file ends up with this coordinator's final data exactly once and no
        timer is left to fire after a reload. A store whose delayed write
        already ran is simply written again with the same data."""
        pending, self._pending_saves = self._pending_saves, {}
        for store, data_func in pending.items():
            try:
                await store.async_save(data_func())
            except Exception:  # noqa: BLE001 — one store must not block the rest
                _LOGGER.exception("AnyVac: could not write %s on unload", store.key)

    def _extract_device(self, coord: Any) -> AnyVacDevice | None:
        """Extract normalised map data for one Roborock v1 coordinator."""
        home = getattr(getattr(coord, "properties_api", None), "home", None)
        if home is None:
            return None

        map_content = _resolve_map_content(coord)

        map_data = getattr(map_content, "map_data", None) if map_content else None
        if map_data is None:
            return None

        # `self` is None in unit tests that call this unbound — keep decimating there.
        data = _extract_map(map_data, decimate=getattr(self, "_expose_legacy_mm", True))
        try:
            data["debug_map"] = _extract_debug(map_data)
        except Exception as err:  # noqa: BLE001 - debug data must never break the update
            _LOGGER.debug("AnyVac: debug extraction failed: %s", err)
            data["debug_map"] = None

        # docs/40 Fáze 0: raw map bytes inventory (length + short hash) only — the
        # bytes THEMSELVES never go on `debug_map` / any sensor attribute, since
        # attributes are pushed over the websocket to every card client. The bytes
        # are only ever written to disk on-demand, by `anyvac.dump_raw_map`
        # (`AnyVacCoordinator.raw_map_for`), never cached here.
        if isinstance(data.get("debug_map"), dict):
            raw = getattr(map_content, "raw_api_response", None) if map_content else None
            if isinstance(raw, (bytes, bytearray)):
                data["debug_map"]["raw_len"] = len(raw)
                data["debug_map"]["raw_sha1"] = hashlib.sha1(raw).hexdigest()[:12]
            else:
                data["debug_map"]["raw_len"] = None
                data["debug_map"]["raw_sha1"] = None

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
        bat = _s("battery")
        data["battery"] = int(bat) if isinstance(bat, (int, float)) else None
        # Passes of the running clean (docs/45 §2.3) — the status only carries
        # `repeat` while a clean runs (verified via diagnostics 2026-09-30).
        rep = _s("repeat")
        data["repeat"] = int(rep) if isinstance(rep, (int, float)) and 1 <= int(rep) <= 3 else None
        # Raw Roborock state (e.g. "washing_the_mop"). NEVER use the HA vacuum entity
        # state for phase detection — a mid-clean mop wash maps to "docked" there
        # (docs/14 rule 4). ``transit`` = in a self-service/driving state.
        state_name = _s("state_name") or getattr(_s("state"), "name", None)
        data["status_state"] = state_name
        # ``transit`` is a *session* concept (mid-clean self-service driving). Outside a
        # cleaning session it must read False — a docked robot is "charging" which is in
        # TRANSIT_STATES, and the dangling True after a clean confused the card's debug
        # view (stale flags). All consumers already condition on in_cleaning.
        data["transit"] = bool(
            data["in_cleaning"] and state_name and state_name in TRANSIT_STATES
        )
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
        }
        # Dock status (docs/26 §1 vrstva A — same `properties_api.status` object
        # already read above for mop_signal, zero new polls/risk). Backs the new
        # portrait footer's Dock sheet (docs/25 §7 field follow-up, 2026-07-24):
        # coarse status flags only (Roborock's API doesn't expose fine-grained
        # tank fill percentages — verified against python-roborock's `DeviceProp`/
        # `DockSummary` dataclasses, which carry dust_collection_mode/wash_towel_mode/
        # smart_wash_params but no tank-level field).
        data["dock_status"] = {
            "dust_collection_status": _s("dust_collection_status"),
            "auto_dust_collection": _s("auto_dust_collection"),
            "water_shortage_status": _s("water_shortage_status"),
            "wash_phase": _s("wash_phase"),
            "wash_ready": _s("wash_ready"),
            "dock_error_status": _s("dock_error_status"),
            # Enum NAME of a set dock error (e.g. "water_empty", docs/47 §3) so the
            # card can say what is wrong; None when there is none.
            "dock_error": _dock_error_name(_s("dock_error_status")),
            "dock_type": _s("dock_type"),
            # Is the dryer running right now? Same field HA 2026.9's own
            # `switch.<vacuum>_mop_drying` reads for its `is_on`.
            "dry_status": _s("dry_status"),
        }
        # Dock CAPABILITIES (2026-09-02). docs/26 §3 said HA/firmware has no
        # documented way to report which dock accessories are installed, so the
        # card gated dock actions on a hand-maintained `dock_type` tier table
        # (`_dockTier`). That is no longer true: `properties_api.device_features
        # .dock_features` is a `RoborockDockFeatures` derived from the dock type
        # by python-roborock itself, and HA 2026.9's own new dock switches
        # (`switch.<vacuum>_dust_emptying` / `_mop_washing` / `_mop_drying`) gate
        # on exactly these three flags. Same object tree as `status` above — no
        # extra poll, no new failure mode. Available in python-roborock 5.31.1
        # too (HA 2026.8); the flags simply went unnoticed until 2026.9 used them.
        #
        # Every flag is a computed property, so read defensively: a library that
        # renames or drops one must degrade to None (card falls back to the
        # `dock_type` tier), never raise mid-poll.
        dock_features = getattr(
            getattr(getattr(coord, "properties_api", None), "device_features", None),
            "dock_features",
            None,
        )

        def _cap(attr: str) -> bool | None:
            if dock_features is None:
                return None
            try:
                val = getattr(dock_features, attr, None)
            except Exception as err:  # noqa: BLE001 - a capability must never break the poll
                _LOGGER.debug("AnyVac: dock feature %s unreadable: %s", attr, err)
                return None
            return bool(val) if val is not None else None

        data["dock_status"]["features"] = {
            "has_dock": _cap("has_dock"),
            "is_collectable": _cap("is_collectable"),
            "is_washable": _cap("is_washable"),
            "is_dryable": _cap("is_dryable"),
        }
        # Which dock action is running right now (docs/14 rule 1 — the card is a
        # view, it does not re-derive state). Mirrors HA 2026.9's dock switches
        # 1:1: emptying/washing come from the raw status state, drying from
        # `dry_status`. `state_name` is the same value already published as
        # `status_state`, so this adds no new source of truth — it just names the
        # three phases the Dock sheet has buttons for.
        data["dock_status"]["running"] = {
            "empty": state_name == "emptying_the_bin" if state_name else False,
            "wash": state_name in WASH_STATES if state_name else False,
            "dry": (
                bool(data["dock_status"]["dry_status"])
                if data["dock_status"]["dry_status"] is not None
                else None
            ),
        }
        # Mop wash cadence (docs/23 §6, docs/26 §3 "vrstva A" item 1) — a separate
        # trait from `status`, only populated for docks that actually support the
        # wash cycle (`dock_features.is_washable`, e.g. S8 MaxV Ultra; None on an
        # empty-only dock like S7 MaxV or no dock at all, S6). Live-verified
        # 2026-07-31 against the user's own S8 (Claude in Chrome, config entry
        # diagnostics JSON — `vacuum.send_command` does not return response data,
        # docs/26 §2 vrstva A caveat, so the diagnostics download was the only way
        # to see the raw payload): ``washInterval`` is in **seconds** (900 == the
        # app's "wash every 15 min" setting, matching the app's 10/15/20/25 min
        # options), NOT minutes — do not "fix" this back to raw minutes.
        # ``smartWash`` is a 0/1 toggle for the app's dirt-sensing auto mode vs.
        # this fixed interval; the planner applies wash_interval_min as a best-
        # effort average cadence either way (docs/23 §6 — no dirt signal available
        # to do better), which is why it's surfaced separately for the card/debug
        # rather than folded silently into a single opaque number.
        smart_wash = getattr(
            getattr(coord, "properties_api", None), "smart_wash_params", None
        )
        wash_interval_s = getattr(smart_wash, "wash_interval", None)
        data["dock_status"]["wash_interval_min"] = (
            round(wash_interval_s / 60, 2)
            if isinstance(wash_interval_s, (int, float))
            else None
        )
        data["dock_status"]["smart_wash_enabled"] = (
            bool(getattr(smart_wash, "smart_wash", None))
            if smart_wash is not None
            else None
        )
        # Dry/wet MODE SIGNAL: "wet" only when a water level is active AND the mop
        # carriage is actually attached (docs/13 B2 — water set + mop pad removed used
        # to record a dry clean as wet). python-roborock only knows
        # `water_box_carriage_status` (0/1) — the `is_water_box_carriage_attached`
        # this once read does not exist (docs/49, P1), so the "no carriage → dry"
        # branch never ran. Unknown attachment (None) keeps the water-mode verdict.
        # This is only the knob: the verdict about a RUN is `resolve_run_kind`.
        water_active = bool(water_name) and not _mode_is_off(water_name)
        # The robot says outright that no water flows (docs/52 O3).
        data["water_off"] = water_name is not None and _mode_is_off(water_name)
        carriage = _s("water_box_carriage_status")
        attached = None if carriage is None else bool(carriage)
        data["clean_type"] = (
            "wet" if water_active and (attached is None or bool(attached)) else "dry"
        )
        # Is the robot actually VACUUMING (suction on)? A mop-only pass (fan "off")
        # must not paint the dry layer / dry coverage (docs/16 field finding: an S8
        # wet clean filled dry cells because its trajectory was counted as dry).
        fan_name = _s("fan_speed_name")
        if not data["in_cleaning"]:
            # Same session-gating as ``transit``: a docked robot still reports its last
            # fan speed, which is not "vacuuming".
            data["vacuuming"] = False
        elif fan_name is not None:
            data["vacuuming"] = not _mode_is_off(fan_name)
        else:
            data["vacuuming"] = data["clean_type"] == "dry"
        vr = data.get("vacuum_room")
        if vr is not None and not data.get("vacuum_room_name"):
            data["vacuum_room_name"] = names.get(vr)

        duid = getattr(coord, "duid", None) or "unknown"
        slug = getattr(coord, "duid_slug", None) or duid
        name = getattr(getattr(coord, "device", None), "name", None) or slug
        return AnyVacDevice(duid=duid, slug=slug, name=name, data=data)
