"""AnyVac services (kontrakt v2, docs/14 §5).

Public command interface for the card (and automations):

- ``anyvac.clean``      — clean intent: rooms + mode (+ vacuums / pin / settings /
                          batch). The backend plans (capability, LPT, pinning),
                          builds a queue of rooms per vacuum and runs it
                          server-side, one room per command (docs/52).
- ``anyvac.edit_job``   — change the running job: add / remove / move / pin rooms.
- ``anyvac.plan``       — the same planner, response-only (assignment preview).
- ``anyvac.goto``       — pin & go; click as PERCENT of the map image, mm math here.
- ``anyvac.zone_clean`` — zone clean; corners as percent, mm math here.
- ``anyvac.cancel``     — tear down running jobs and send the started robots home.
- ``anyvac.select_rooms`` / ``anyvac.pin_room`` / ``anyvac.set_layers`` /
  ``anyvac.reset_learning`` — state. (``anyvac.set_room_sequence`` was removed in
  1.60.0: room-by-room jobs choose their own order; a batch dispatch reads the
  app's order from the robot, docs/52 O1.)
  (``anyvac.run_job``, the old raw-task-list executor, was removed in 1.52.1: it
  called any service with any data and never carried the caller's context. Jobs
  start through ``anyvac.clean`` and the planner only.)
- ``anyvac.snapshot_map_as_floorplan`` / ``anyvac.export_map_guide`` — custom
                          floorplan helpers (docs/30 §8, docs/37): save a
                          vacuum's own map as a static photo, and export its
                          room/dry/wet geometry as transparent tracing layers
                          over that same crop. Neither writes card config.
- ``anyvac.dump_raw_map`` — DEBUG/DIAGNOSTIC only (docs/40 Fáze 0): writes a
                          vacuum's raw Roborock map bytes to disk for the
                          offline home-frame registration probe. No card
                          involvement, no behaviour change to anything else.
- ``anyvac.probe_map_rate`` — DEBUG/DIAGNOSTIC only (docs/47 §4): fetches one
                          vacuum's map repeatedly for a while and reports
                          latency, size and whether position/trace changed,
                          to measure if fetching faster than the official
                          integration's 30 s is worth it. Nothing is kept.
- ``anyvac.snap_wall_corner`` — docs/40 §5.B: given a point in home-frame px,
                          returns the nearest wall-corner vertex found in
                          that frame's own wall mask (or the point unchanged
                          if the frame has no walls yet). Used by the card's
                          foreign-floorplan N-point calibration to remove
                          most click noise on the home-frame side of a pair.

Execution model (docs/52): a job is a queue of rooms per vacuum
(`jobrunner.JobRunner`). Each robot gets one room per command, the next when the
firmware reports the previous one finished; a wet room waits for the dry pass of
the same room. Starting a new job cancels the previous one (docs/13 C6 — no
double-driving robots).
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
from urllib.parse import quote
import re
import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
import voluptuous as vol

from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import livediff, localprobe
from . import reach as reachmod
from .const import DOMAIN
from .jobrunner import JobRunner, _active_jobs as _runner_jobs
from .planner import CleanPlanner, duid_for_entity, vacuum_entity_for_duid

_LOGGER = logging.getLogger(__name__)

SERVICE_SELECT_ROOMS = "select_rooms"
SERVICE_PIN_ROOM = "pin_room"
SERVICE_SET_LAYERS = "set_layers"
SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_CLEAN = "clean"
SERVICE_PLAN = "plan"
SERVICE_GOTO = "goto"
SERVICE_ZONE_CLEAN = "zone_clean"
SERVICE_CANCEL = "cancel"
SERVICE_EDIT_JOB = "edit_job"
SERVICE_DOCK_EMPTY = "dock_empty"
SERVICE_DOCK_WASH = "dock_wash"
SERVICE_DOCK_DRY = "dock_dry"
SERVICE_DOCK_PUMP = "dock_pump"
SERVICE_DOCK_SELF_CLEAN = "dock_self_clean"
SERVICE_DOCK_RESOLVE_ERROR = "dock_resolve_error"
SERVICE_PROBE_MAP_RATE = "probe_map_rate"
SERVICE_SNAPSHOT_FLOORPLAN = "snapshot_map_as_floorplan"
SERVICE_EXPORT_MAP_GUIDE = "export_map_guide"
SERVICE_DUMP_RAW_MAP = "dump_raw_map"
SERVICE_SNAP_WALL_CORNER = "snap_wall_corner"
SERVICE_DETECT_FIDUCIALS = "detect_floorplan_fiducials"
SERVICE_SET_FLOORPLAN_SEAT = "set_floorplan_seat"
SERVICE_LIST_ASSETS = "list_assets"
SERVICE_RESET_REACH = "reset_reach"
SERVICE_FLOORPLAN_SCALE = "floorplan_scale"
SERVICE_UPLOAD_ASSET = "upload_asset"

# docs/51 §1.4: config/www-relative folder the studio's Furniture tool lists.
FURNITURE_DIR = ("anyvac", "furniture")
FURNITURE_EXTS = (".webp", ".png", ".svg", ".jpg", ".jpeg")

# 1.51.9 (plan po auditu, C2): config-relative folder for `dump_raw_map`.
DEBUG_DUMP_DIR = "anyvac/debug"

# 1.51.9 (plan po auditu, C3): services that read or write files in the config
# directory or are diagnostics. Only an administrator may call them — the same
# rule Home Assistant applies to its own file and debug services. Every one is
# used only from setup flows (calibration, map guide) or by hand.
ADMIN_SERVICES: frozenset[str] = frozenset(
    {
        SERVICE_SNAPSHOT_FLOORPLAN,
        SERVICE_EXPORT_MAP_GUIDE,
        SERVICE_DUMP_RAW_MAP,
        SERVICE_PROBE_MAP_RATE,
        SERVICE_DETECT_FIDUCIALS,
        SERVICE_LIST_ASSETS,
        SERVICE_UPLOAD_ASSET,
    }
)

ALL_SERVICES = (
    SERVICE_SELECT_ROOMS,
    SERVICE_PIN_ROOM,
    SERVICE_SET_LAYERS,
    SERVICE_EDIT_JOB,
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
    SERVICE_DOCK_RESOLVE_ERROR,
    SERVICE_PROBE_MAP_RATE,
    SERVICE_SNAPSHOT_FLOORPLAN,
    SERVICE_EXPORT_MAP_GUIDE,
    SERVICE_DUMP_RAW_MAP,
    SERVICE_SNAP_WALL_CORNER,
    SERVICE_DETECT_FIDUCIALS,
    SERVICE_SET_FLOORPLAN_SEAT,
    SERVICE_LIST_ASSETS,
    SERVICE_RESET_REACH,
    SERVICE_FLOORPLAN_SCALE,
    SERVICE_UPLOAD_ASSET,
)

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


def _finite_float(value: Any) -> float:
    """Coerce to float, rejecting NaN/Infinity (docs/41 §4.6 "čísla konečná") —
    plain `vol.Coerce(float)` lets both through, and `vol.Range` does not catch
    them either (NaN compares False against both bounds; an unbounded field like
    `rotation`/`offset_x`/`offset_y` has no Range at all)."""
    v = float(value)
    if not math.isfinite(v):
        raise vol.Invalid("must be a finite number")
    return v


def _positive_finite_float(value: Any) -> float:
    v = _finite_float(value)
    if v <= 0:
        raise vol.Invalid("must be > 0")
    return v


# docs/41 §4.6 — Align mode manual floorplan seating, stored as a backend
# override layer (see AnyVacCoordinator.set_floorplan_seat). `map` fields
# mirror the card's `SeatParams` (camelCase `scaleY` there is `scale_y` here,
# same as the existing `vacuums[].map` config shape).
_FLOORPLAN_SEAT_MAP_SCHEMA = vol.Schema(
    {
        vol.Required("rotation"): _finite_float,
        vol.Required("scale"): _positive_finite_float,
        vol.Optional("scale_y"): _positive_finite_float,
        vol.Required("offset_x"): _finite_float,
        vol.Required("offset_y"): _finite_float,
    }
)
# docs/42 §9 (fáze pre-H) — Visual editor's Seat & Appearance tool persists
# its Appearance fields the same way it already persists the seat geometry
# above: as a backend override, keyed the same way, on the same per-vacuum
# entry. These 11 fields mirror the card's `VacuumConfig` Appearance fields
# 1:1 (types.ts) — nothing here is computed or interpreted, only validated
# and stored opaquely, same posture as `map`.
_FLOORPLAN_SEAT_APPEARANCE_SCHEMA = vol.Schema(
    {
        vol.Optional("hide_map"): bool,
        vol.Optional("overlay_opacity"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("overlay_blend"): vol.In(["normal", "lighten", "screen", "plus-lighter"]),
        vol.Optional("path_color"): vol.Any(str, None),
        vol.Optional("path_width"): vol.All(vol.Coerce(float), vol.Range(min=20, max=300)),
        vol.Optional("mop_path_color"): vol.Any(str, None),
        vol.Optional("mop_band_opacity"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("mop_band_width"): vol.All(vol.Coerce(float), vol.Range(min=20, max=400)),
        vol.Optional("robot_image_on_map"): bool,
        vol.Optional("robot_size"): vol.All(vol.Coerce(float), vol.Range(min=40, max=220)),
        vol.Optional("robot_image_rotation"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
    }
)
# docs/42 §4.4/§8 bod 1 (fáze K) — one room's Rooms-tool override. Mirrors the
# card's `RoomConfig` rect/anchor fields 1:1 (types.ts `map_x/map_y/map_w/map_h`,
# `area_id`) — all optional since a caller may set only the geometry, only
# `area_id`, or both together. `map_x`/`map_y` are unbounded (a room's anchor can
# legitimately sit outside 0–100% while its dashboard is mid-drag/mid-resize,
# same posture as `map`'s `offset_x`/`offset_y`); `map_w`/`map_h` must be
# strictly positive, same posture as `map`'s `scale`.
_FLOORPLAN_SEAT_ROOM_SCHEMA = vol.Schema(
    {
        vol.Optional("map_x"): _finite_float,
        vol.Optional("map_y"): _finite_float,
        vol.Optional("map_w"): _positive_finite_float,
        vol.Optional("map_h"): _positive_finite_float,
        vol.Optional("area_id"): vol.Any(str, None),
    }
)
# docs/42 §4.4 (fáze K) — `rooms` is a MAP of room_key -> (room override |
# `null`), unlike `map`/`appearance` which are each a single atomic override.
# Each key is independent: a dict sets/replaces that one room's override, `null`
# clears just that one room's override, and a room_key simply not mentioned in
# a given call is left completely untouched (see `set_floorplan_seat`'s
# docstring in coordinator.py for why this deliberately does NOT follow the
# map/appearance "omit the whole field to clear it" convention).
_FLOORPLAN_SEAT_ROOMS_SCHEMA = vol.Schema({str: vol.Any(_FLOORPLAN_SEAT_ROOM_SCHEMA, None)})
# docs/42 §3.3/§9 (fáze I addendum, found while implementing the Rooms tool's
# border-width sliders — not itself in the original §4.4 schema) — the two
# GLOBAL border-width fields (`room_border_normal`/`room_border_selected`,
# `types.ts`, previously only settable as plain YAML on the card config) are
# card-level only (they apply to every vacuum, there is no per-vacuum notion
# of "this vacuum's room border width") and, like `image_base`, the Rooms
# tool can only persist them through this backend override layer — the
# Visual editor is openable from a live dashboard, where the card cannot
# write its own YAML (docs/41 §0), the same reason `appearance`/`rooms`
# already go through this service instead of a `config-changed` event. Reuses
# the editor's existing slider bounds (0–12 px, `editor.ts` `_numberSlider`
# calls) rather than inventing new ones.
_FLOORPLAN_SEAT_ROOM_STYLE_SCHEMA = vol.Schema(
    {
        vol.Optional("border_normal"): vol.All(vol.Coerce(float), vol.Range(min=0, max=12)),
        vol.Optional("border_selected"): vol.All(vol.Coerce(float), vol.Range(min=0, max=12)),
    }
)
SET_FLOORPLAN_SEAT_SCHEMA = vol.Schema(
    {
        vol.Required("floorplan"): str,
        # Omitted → this call is about the card-level `image_base` override
        # instead of a per-vacuum `map`/`appearance`/`rooms` override (docs/41 §4.6).
        vol.Optional("vacuum"): str,
        vol.Optional("map"): vol.Any(_FLOORPLAN_SEAT_MAP_SCHEMA, None),
        # docs/42 — independent of `map`; the card's Visual editor always
        # sends both together on Save (current draft state for each), but
        # this field can be set/cleared on its own. Omitted (like `None`)
        # clears any existing appearance override for this vacuum — see
        # `AnyVacCoordinator.set_floorplan_seat`'s docstring and the
        # `appearance` field's own description in services.yaml.
        vol.Optional("appearance"): vol.Any(_FLOORPLAN_SEAT_APPEARANCE_SCHEMA, None),
        # docs/42 §4.4 (fáze K) — independent of `map`/`appearance`, and with
        # its OWN merge semantics (per room_key, see _FLOORPLAN_SEAT_ROOMS_SCHEMA
        # above) rather than the whole-field "omit = clear" rule `map`/
        # `appearance` use. Omitting `rooms` entirely from a call (e.g. a plain
        # seat-geometry Save from the Seat & Appearance tool) leaves every
        # existing room override untouched. Valid with `vacuum` given (split
        # mode, per-vacuum rooms) OR omitted (merged mode, card-level rooms —
        # same "vacuum given = per-vacuum, omitted = card-level" split
        # `image_base` already uses); see `AnyVacCoordinator
        # .set_floorplan_seat`'s docstring for how it interacts with
        # `image_base` when both apply to a card-level call.
        vol.Optional("rooms"): _FLOORPLAN_SEAT_ROOMS_SCHEMA,
        # docs/42 §3.3/§9 (fáze I addendum) — card-level only (ignored when
        # `vacuum` is given, same as `image_base`); follows the SAME atomic
        # "no sentinel" contract as `image_base`/`map`/`appearance`: omitted
        # (or explicit `null`) clears any existing `room_style` override, so
        # a card-level call that wants to KEEP it (e.g. a pure rooms-geometry
        # or image_base Save) must resend the current draft every time — see
        # `AnyVacCoordinator.set_floorplan_seat`'s docstring.
        vol.Optional("room_style"): vol.Any(_FLOORPLAN_SEAT_ROOM_STYLE_SCHEMA, None),
        # Opaque here (crop_box/home_anchors, docs/41 phase G) — stored and
        # returned as-is, never interpreted at this layer.
        vol.Optional("image_base"): vol.Any(dict, None),
    }
)
RESET_REACH_SCHEMA = vol.Schema(
    {
        vol.Optional("duid"): str,
        vol.Optional("room"): str,
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
        # Vacuums that get all their rooms in one command, in the order set in
        # the Roborock app (docs/52 R7). Their part of the job cannot be
        # changed once sent.
        vol.Optional("batch"): [str],
    }
)
_KIND = vol.In(["dry", "wet"])
EDIT_JOB_SCHEMA = vol.Schema(
    {
        vol.Optional("add"): [str],
        vol.Optional("remove"): [str],
        # Pass of the rooms in `add` / `remove` (default: the job's mode / both).
        vol.Optional("mode"): vol.In(["dry", "wet", "both"]),
        vol.Optional("move"): vol.Schema(
            {
                vol.Required("room"): str,
                vol.Optional("kind", default="dry"): _KIND,
                vol.Required("index"): vol.All(vol.Coerce(int), vol.Range(min=0)),
            }
        ),
        vol.Optional("pin"): {str: vol.Schema({vol.Optional("dry"): str, vol.Optional("wet"): str})},
        vol.Optional("settings"): vol.Schema(
            {
                vol.Optional("dry"): {str: _SETTINGS_KIND_SCHEMA},
                vol.Optional("wet"): {str: _SETTINGS_KIND_SCHEMA},
            }
        ),
    }
)
# docs/40 §4.3 (Fáze 2): `frame: "home"` targets the point in the SHARED home
# frame's px space instead of the target robot's own rendered-map percent
# space — the card re-normalises a floorplan-% click through `crop_box` into
# home px (same class of computation as `placeRoomInCrop`, kánon docs/14) and
# never computes robot mm itself; the backend inverts the registration
# (`coordinator.home_px_to_mm`, Fáze 1) to get there. `x_pct`/`y_pct` stay
# Optional (not Required) so `frame: "home"` callers can omit them entirely —
# `_target_mm_for` below enforces that exactly one coordinate style is given,
# with a clearer error than vol.Exclusive/vol.Inclusive would produce.
GOTO_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Optional("frame", default="robot"): vol.In(["robot", "home"]),
        vol.Optional("x_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("y_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("x_home_px"): vol.Coerce(float),
        vol.Optional("y_home_px"): vol.Coerce(float),
    }
)
ZONE_CLEAN_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Optional("frame", default="robot"): vol.In(["robot", "home"]),
        vol.Optional("x1_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("y1_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("x2_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("y2_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("x1_home_px"): vol.Coerce(float),
        vol.Optional("y1_home_px"): vol.Coerce(float),
        vol.Optional("x2_home_px"): vol.Coerce(float),
        vol.Optional("y2_home_px"): vol.Coerce(float),
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
# Empty / wash / dry are cycles the dock runs for a while and ends on its own, so
# they also have a stop (2026-09-02). HA 2026.9 shipped native switches for exactly
# these three (`switch.<vacuum>_dust_emptying` / `_mop_washing` / `_mop_drying`)
# using the same command pairs; AnyVac keeps its own services because the backend
# stays the single writer (docs/14 rule 1), but there is no reason for it to be the
# poorer interface. Pump and self-clean have no documented stop and keep the plain
# schema above.
DOCK_TOGGLE_SCHEMA = DOCK_ACTION_SCHEMA.extend(
    {vol.Optional("action", default="start"): vol.In(["start", "stop"])}
)
# docs/40 Fáze 0 (2026-09-13): DEBUG/DIAGNOSTIC ONLY — dumps a vacuum's raw
# Roborock map bytes to disk for the offline home-frame registration probe
# (`anyvac/tools/homeframe_probe.py`). Same target resolution as goto/zone_clean/
# the dock actions (entity_id or duid).
# docs/47 §4. Every map request goes over the Roborock CLOUD (python-roborock's
# map RPC channel is MQTT-only, even when the robot is connected locally), so
# the floor of 3 s and the 5 minute cap keep a probe from hammering it.
PROBE_INTERVAL_MIN_S = 3.0
PROBE_DURATION_MAX_S = 300.0
PROBE_MAP_RATE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Optional("duration_s", default=120): vol.All(
            vol.Coerce(float), vol.Range(min=10, max=PROBE_DURATION_MAX_S)
        ),
        vol.Optional("interval_s", default=10): vol.All(
            vol.Coerce(float), vol.Range(min=PROBE_INTERVAL_MIN_S, max=60)
        ),
        vol.Optional("dynamic_diff", default=False): bool,
        vol.Optional("transport", default="local"): vol.In(["local", "cloud"]),
        vol.Optional("map_every", default=1): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
        # docs/48 §4.2 experiment: params sent with get_dynamic_map_diff
        # (local transport only), e.g. [0] or {"nonce": 0}.
        vol.Optional("diff_params"): vol.Any(list, dict, int, str),
    }
)

DUMP_RAW_MAP_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): str,
        vol.Optional("duid"): str,
        vol.Optional("name"): str,
    }
)
# docs/40 §5.B: the card resolves a click into home-frame px itself (same
# floorplan-% -> home-px re-normalisation `frame: "home"` goto/zone_clean
# already do, kánon docs/14 — no new client-side geometry), then asks the
# backend to snap that point to the nearest actual wall corner it can see in
# the frame's own wall mask. `frame_id` is Optional for the same reason as
# `snapshot_map_as_floorplan`'s: omit it to use the frame with the most
# registered vacuums.
SNAP_WALL_CORNER_SCHEMA = vol.Schema(
    {
        vol.Optional("frame_id"): str,
        vol.Required("x_home_px"): vol.Coerce(float),
        vol.Required("y_home_px"): vol.Coerce(float),
    }
)
# docs/40 §5.A.2: pairs with `snapshot_map_as_floorplan`'s `fiducials: true` —
# `fiducials` here is exactly the `{id, home_px}` list that call returned when
# it embedded the markers (the card just threads it through unmodified), so
# this service never has to re-derive or store where a marker was placed;
# `path` is the (possibly now cropped/resized/rotated) floorplan file to scan,
# same `/local/anyvac/...` shape every snapshot/upload already produces.
DETECT_FIDUCIALS_SCHEMA = vol.Schema(
    {
        vol.Required("path"): str,
        vol.Required("fiducials"): [
            vol.Schema(
                {
                    vol.Required("id"): str,
                    vol.Required("home_px"): vol.Schema(
                        {
                            vol.Required("x"): vol.Coerce(float),
                            vol.Required("y"): vol.Coerce(float),
                        }
                    ),
                }
            )
        ],
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
# docs/40 §4.3 (Fáze 2): `frame: "home"` renders a COMPOSITE of every
# vacuum registered into a shared home frame instead of photographing one
# vacuum's own rendered map — `image_entity` is then unused (there is no
# single image entity for "all of them at once"). `image_entity` stays
# Optional so a `frame: "home"` call can omit it entirely; the handler
# enforces exactly one of {image_entity} / {frame: "home"}, same pattern as
# goto/zone_clean's `_target_mm_for`.
SNAPSHOT_FLOORPLAN_SCHEMA = vol.Schema(
    {
        vol.Optional("image_entity"): str,
        vol.Optional("frame"): vol.In(["home"]),
        vol.Optional("frame_id"): str,
        vol.Optional("name"): str,
        # docs/40 §5.A.2: opt-in, `frame: "home"` only (ignored otherwise — a
        # per-vacuum photographed map has no frame-px coordinate space to
        # place a marker in). Embeds 4 invisible fiducial markers in the
        # snapshot's own padding border; response gains a `fiducials` list
        # the card threads straight into `anyvac.detect_floorplan_fiducials`
        # later, once the file has been cropped/resized externally.
        vol.Optional("fiducials", default=False): bool,
    }
)
# docs/37: draws room-boundary / dry-path / wet-path guides as transparent PNGs
# in the SAME pixel canvas as `snapshot_map_as_floorplan`'s crop, so a user
# building a custom floorplan in an external image editor (GIMP etc.) can lay
# them over the floorplan photo as tracing layers — the negative space inside
# the drawn path is where furniture stands. Draws only, never touches config
# (docs/37 §2 point 4 — unlike the snapshot service, this has no side effects).
# docs/40 §4.3 (Fáze 2): `frame: "home"` draws layers for EVERY aligned
# vacuum on one shared canvas (real room outlines, `outline_home_px`,
# instead of one vacuum's bboxes) — `image_entity` is then unused, same
# reasoning as `SNAPSHOT_FLOORPLAN_SCHEMA` above.
EXPORT_MAP_GUIDE_SCHEMA = vol.Schema(
    {
        vol.Optional("image_entity"): str,
        vol.Optional("frame"): vol.In(["home"]),
        vol.Optional("frame_id"): str,
        vol.Optional("name"): str,
        vol.Optional("layers", default=["rooms", "dry", "wet"]): [
            vol.In(["rooms", "dry", "wet", "reach"])
        ],
        vol.Optional("labels", default=True): bool,
        vol.Optional("stroke_mm", default=300): vol.All(
            vol.Coerce(int), vol.Range(min=50, max=600)
        ),
        vol.Optional("crop"): vol.Schema(
            {
                vol.Required("x0"): vol.Coerce(float),
                vol.Required("y0"): vol.Coerce(float),
                vol.Required("x1"): vol.Coerce(float),
                vol.Required("y1"): vol.Coerce(float),
            }
        ),
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


# ── Raw map dump (docs/40 Fáze 0) ──────────────────────────────────────────────


def _raw_map_filename(name: str, map_flag: Any) -> str:
    """Pure helper (mirrors `_floorplan_filename`): slugified `<name>_<map_flag>.bin`
    filename for one `anyvac.dump_raw_map` dump. `map_flag` distinguishes a
    multi-map vacuum's floors from each other; falls back to "map" when it is
    unknown (e.g. a Roborock/library version with no `current_map_data`)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "vacuum"
    flag_slug = (
        re.sub(r"[^a-z0-9]+", "_", str(map_flag).lower()).strip("_")
        if map_flag is not None
        else ""
    ) or "map"
    return f"{slug}_{flag_slug}.bin"


def _require_raw_map(coords: list[Any], duid: str) -> tuple[bytes, dict[str, Any]]:
    """Look up raw map bytes for `duid` across every AnyVac coordinator (in
    practice one), via `AnyVacCoordinator.raw_map_for` — no second
    implementation of that piggyback walk here (docs/14 rule 1). Raises
    `ServiceValidationError` with a clear message when none has it yet, so
    `anyvac.dump_raw_map`'s "no raw data available" path is unit-testable
    without a running Home Assistant instance (mock objects exposing just
    `raw_map_for`, no real coordinator needed)."""
    for coord in coords:
        result = coord.raw_map_for(duid)
        if result is not None:
            return result
    raise ServiceValidationError(
        f"anyvac.dump_raw_map: no raw map bytes available yet for vacuum '{duid}' "
        "(piggyback map not ready, or this Roborock integration/library version "
        "does not expose raw_api_response)"
    )


# docs/30 §4a second field follow-up (2026-07-30): a Roborock map image's
# canvas is much larger than the actually-explored area — the surrounding
# "unexplored" fill is included in a raw snapshot, so a floorplan built from
# one looks mostly empty and wastes the crop for auto-seating precision.
# Cropping to a fixed aspect ratio or trimming by pixel colour would be a
# fragile guess; the union of the vacuum's own known room bounding boxes
# (`rooms[].bbox_px`, already computed by the coordinator from the SAME
# image/coordinate space) is exact, so that's what's used instead.
FLOORPLAN_CROP_PADDING_FRAC = 0.08  # cosmetic breathing room, not a safety margin


def _room_union_bbox_px(rooms: list[Any]) -> tuple[float, float, float, float] | None:
    """Pure helper: union bounding box (x0, y0, x1, y1) in image pixel space
    across every room that currently has one. None if no room has a usable
    `bbox_px` yet (e.g. right after a remap, before the first poll re-derives
    it) — caller falls back to the uncropped image in that case."""
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    for room in rooms:
        bbox = room.get("bbox_px") if isinstance(room, dict) else None
        if not isinstance(bbox, dict):
            continue
        x0, y0, x1, y1 = bbox.get("x0"), bbox.get("y0"), bbox.get("x1"), bbox.get("y1")
        if x0 is None or y0 is None or x1 is None or y1 is None:
            continue
        xs0.append(x0)
        ys0.append(y0)
        xs1.append(x1)
        ys1.append(y1)
    if not xs0:
        return None
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _padded_crop_box(
    bbox: tuple[float, float, float, float], img_w: int, img_h: int
) -> tuple[int, int, int, int]:
    """Pure helper: pads `bbox` by `FLOORPLAN_CROP_PADDING_FRAC` of its own
    size on each side and clamps to the image bounds, returning a
    (left, top, right, bottom) int box ready for `Image.crop()`."""
    x0, y0, x1, y1 = bbox
    pad_x = (x1 - x0) * FLOORPLAN_CROP_PADDING_FRAC
    pad_y = (y1 - y0) * FLOORPLAN_CROP_PADDING_FRAC
    left = max(0, int(x0 - pad_x))
    top = max(0, int(y0 - pad_y))
    right = min(img_w, int(x1 + pad_x))
    bottom = min(img_h, int(y1 + pad_y))
    return left, top, right, bottom


def _crop_image_to_bbox(
    content: bytes, bbox: tuple[float, float, float, float]
) -> tuple[bytes, str, tuple[int, int, int, int]]:
    """Blocking (run via executor) — opens `content` as an image, crops to a
    padded box around `bbox` (the room union, in the image's own pixel
    space), and returns (new_bytes, content_type, box). `box` is the exact
    (left, top, right, bottom) crop applied, in the SAME pixel space as the
    `bbox_px` the caller passed in — the card needs this back to place the
    snapshotted vacuum's own rooms onto the now-cropped image without any
    manual dragging (docs/30 §8 "big seating rework": identity placement
    against the full uncropped canvas is wrong once the saved file is a
    crop of it). Re-encodes as PNG regardless of the source format: this is
    a one-off local floorplan file, not something needing source-format
    fidelity. Raises on any failure (unreadable image, Pillow missing,
    etc.) — the caller treats cropping as best-effort and falls back to the
    uncropped snapshot (and no room auto-placement)."""
    from PIL import Image  # HA core depends on Pillow; imported lazily so a
    # missing/broken install only breaks this optional crop step, never the
    # rest of the service (mirrors the lazy `async_get_image` import above).

    with Image.open(io.BytesIO(content)) as im:
        box = _padded_crop_box(bbox, im.width, im.height)
        cropped = im.crop(box)
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue(), "image/png", box


# ── Home frame composite (docs/40 §4.3, Fáze 2) ───────────────────────────────


def _select_home_frame(
    hass: HomeAssistant, frame_id: str | None, *, service: str
) -> tuple[str, dict[str, Any]]:
    """Resolve which home frame `frame: "home"` composites (shared by
    `snapshot_map_as_floorplan` and `export_map_guide`, docs/14 rule 1 — one
    selection policy, not two). An explicit `frame_id` picks that frame
    outright (raises if unknown — a typo'd id must never silently fall back
    to a different apartment's frame). Otherwise picks the frame with the
    most registered robots among the non-`stale` ones: the "main" home is
    overwhelmingly the common case (one frame with N robots, everything else
    a stray `unaligned` single-robot frame from a different floor), and
    robot-count is a simple, stable proxy for that without needing the user
    to know frame ids for the everyday call."""
    frames: dict[str, dict[str, Any]] = {}
    for coord in _coordinators(hass):
        frames.update(coord.home_frames_snapshot())
    if frame_id:
        frame = frames.get(frame_id)
        if frame is None:
            raise HomeAssistantError(f"anyvac.{service}: unknown frame_id '{frame_id}'")
        return frame_id, frame
    candidates = [(fid, f) for fid, f in frames.items() if not f.get("stale")]
    if not candidates:
        raise HomeAssistantError(
            f'anyvac.{service}: frame: "home" requires at least one vacuum with a '
            "home-frame registration (see the 'home_frame'/'registration' sensor "
            "attributes) — none exists yet"
        )
    return max(candidates, key=lambda kv: len(kv[1].get("robots") or {}))


def _snap_wall_corner(
    frame_id: str, frame: dict[str, Any], x_home_px: float, y_home_px: float
) -> dict[str, Any]:
    """Blocking (run via `hass.async_add_executor_job`, like
    `_home_frame_composite_png`) — pure px<->mm shuttle around
    `homeframe.nearest_wall_corner_mm` (docs/40 §5.B): converts the click
    into frame mm, asks for the nearest wall-corner vertex, converts the
    result back to home px. `snapped: False` (point echoed back unchanged)
    when the frame has no wall data yet, so the card can fall back to the
    raw click rather than fail the calibration step outright. `distance_px`
    lets the card show how far the click actually moved, the same kind of
    visible-effect feedback `_calibPreview`'s live fit-error already gives
    docs/39's per-robot flow."""
    from .homeframe import HOME_PX_SCALE, home_px_to_mm, mm_to_home_px, nearest_wall_corner_mm

    cell_mm = frame.get("cell_mm", 50)
    scale = frame.get("scale") or HOME_PX_SCALE
    x_mm, y_mm = home_px_to_mm(frame["origin_mm"], cell_mm, scale, x_home_px, y_home_px)
    snapped_mm = nearest_wall_corner_mm(frame, x_mm, y_mm)
    if snapped_mm is None:
        return {
            "frame_id": frame_id,
            "snapped": False,
            "x_home_px": x_home_px,
            "y_home_px": y_home_px,
        }
    sx_px, sy_px = mm_to_home_px(frame["origin_mm"], cell_mm, scale, snapped_mm[0], snapped_mm[1])
    distance_px = math.hypot(sx_px - x_home_px, sy_px - y_home_px)
    return {
        "frame_id": frame_id,
        "snapped": True,
        "x_home_px": round(sx_px, 1),
        "y_home_px": round(sy_px, 1),
        "distance_px": round(distance_px, 1),
    }


def _home_frame_occupied_crop_px(frame: dict[str, Any]) -> tuple[int, int, int, int]:
    """Pure helper shared by `_home_frame_composite_png` (the snapshot's
    background) and `export_map_guide`'s `frame: "home"` branch (which draws
    only transparent tracing layers, no background — docs/14 rule 1, one
    crop-box policy for both): the frame's occupied extent (any floor OR
    wall cell across every robot merged into it), converted to home px and
    padded/clamped the SAME way a photographed snapshot's crop is
    (`_padded_crop_box`) — so a guide layer exported without an explicit
    `crop` lines up with a `frame: "home"` snapshot taken with no explicit
    `frame_id`/crop either."""
    from .homeframe import HOME_PX_SCALE

    floor = frame["floor_mask"]
    wall = frame["wall_mask"]
    height, width = floor.shape
    occupied = floor | wall
    ys, xs = np.nonzero(occupied)
    if ys.size == 0:
        bbox_cells = (0, 0, width, height)
    else:
        bbox_cells = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    scale = frame.get("scale") or HOME_PX_SCALE
    full_w = max(1, round(width * scale))
    full_h = max(1, round(height * scale))
    bbox_px = tuple(v * scale for v in bbox_cells)
    return _padded_crop_box(bbox_px, full_w, full_h)


# docs/40 §5.A.2 — how far a marker sits inset from each corner of the crop
# box, as a fraction of the box's own smaller dimension, and the clamped
# range for its drawn half-side (both in home px). Kept small enough to sit
# inside `FLOORPLAN_CROP_PADDING_FRAC`'s (8%) border on a typical home, large
# enough to survive a moderate resize.
FIDUCIAL_INSET_FRAC = 0.035
FIDUCIAL_MARKER_HALF_PX_RANGE = (4, 20)


def _fiducial_marker_specs(box: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    """Pure helper: where `_embed_fiducial_markers` draws each of the 4
    fiducial markers (docs/40 §5.A.2), in the SAME home-px coordinate space
    `box` and every other `*_home_px` value already uses — i.e. BEFORE the
    crop, so a marker's reported position is a normal absolute point on the
    frame, unaffected by which crop box a particular snapshot happened to
    use. Inset from each corner of `box` so markers land inside the cosmetic
    padding border around the occupied floor, not on top of real floor/wall
    pixels — though on a home whose explored area already reaches the frame
    canvas edge (pad clamped near 0 on that side, see `_padded_crop_box`) a
    marker can still land on real content; harmless for detection (colour,
    not position, identifies a marker) but not perfectly invisible in that
    corner — an accepted rough edge of a deliberately cheap, opt-in feature.
    Returns `[{"id", "x", "y", "half"}, ...]`, one per corner."""
    left, top, right, bottom = box
    w, h = right - left, bottom - top
    inset = min(w, h) * FIDUCIAL_INSET_FRAC
    lo, hi = FIDUCIAL_MARKER_HALF_PX_RANGE
    half = max(lo, min(hi, round(min(w, h) * 0.01)))
    corners = {
        "tl": (left + inset, top + inset),
        "tr": (right - inset, top + inset),
        "bl": (left + inset, bottom - inset),
        "br": (right - inset, bottom - inset),
    }
    return [{"id": mid, "x": x, "y": y, "half": half} for mid, (x, y) in corners.items()]


def _embed_fiducial_markers(img: Any, box: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    """Draws the 4 markers `_fiducial_marker_specs` places onto `img` (the
    FULL, pre-crop composite canvas `_home_frame_composite_png` builds) at
    `FIDUCIAL_MARKER_ALPHA` — near-fully-transparent, so invisible once the
    file is viewed/composited normally, but recoverable at the raw-pixel
    level by `homeframe.find_fiducial_markers` as long as the alpha channel
    survives whatever the user does to the file afterwards. Mutates `img`
    (a PIL RGBA Image) in place; returns `[{"id", "home_px"}, ...]` ready to
    hand straight back in the service response."""
    from PIL import ImageDraw

    from .homeframe import FIDUCIAL_MARKER_ALPHA, FIDUCIAL_MARKER_COLORS

    draw = ImageDraw.Draw(img)
    out: list[dict[str, Any]] = []
    for spec in _fiducial_marker_specs(box):
        mid, x, y, half = spec["id"], spec["x"], spec["y"], spec["half"]
        r, g, b = FIDUCIAL_MARKER_COLORS[mid]
        draw.rectangle([x - half, y - half, x + half, y + half], fill=(r, g, b, FIDUCIAL_MARKER_ALPHA))
        out.append({"id": mid, "home_px": {"x": x, "y": y}})
    return out


def _home_frame_composite_png(
    frame: dict[str, Any], fiducials: bool = False
) -> tuple[bytes, tuple[int, int, int, int], list[dict[str, Any]]]:
    """Blocking (run via `hass.async_add_executor_job`, like
    `_crop_image_to_bbox`) — but RENDERS a floorplan PNG from the frame's own
    `floor_mask`/`wall_mask` rasters instead of cropping an existing
    photographed one: there is no single image entity for "every vacuum
    registered into this frame at once", so this is the composite itself,
    not a crop of somebody's photo. Builds the RGBA canvas at the frame's
    native cell resolution (cheap — one boolean-mask assignment per colour,
    never a per-cell Python loop) and upscales by `HOME_PX_SCALE` with
    NEAREST resampling (a floorplan background, not a photo — no smoothing
    across cell boundaries). Crops to `_home_frame_occupied_crop_px` (shared
    with `export_map_guide`'s home-frame branch, docs/14 rule 1). When
    `fiducials` is set (docs/40 §5.A.2), draws the 4 invisible markers onto
    the canvas BEFORE cropping (`_embed_fiducial_markers`) — same crop box,
    so their reported home_px lines up with everything else `frame: "home"`
    publishes. Returns (png_bytes, crop_box, markers) with crop_box =
    (left, top, right, bottom) in home px and markers = `[]` unless
    `fiducials` was set."""
    from PIL import Image

    from .homeframe import HOME_PX_SCALE

    floor = frame["floor_mask"]
    wall = frame["wall_mask"]
    height, width = floor.shape

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[floor] = (235, 235, 235, 255)  # light grey floor
    rgba[wall] = (60, 60, 60, 255)  # dark grey wall (drawn after floor: disjoint anyway)

    scale = frame.get("scale") or HOME_PX_SCALE
    img = Image.fromarray(rgba, mode="RGBA").resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.NEAREST
    )
    box = _home_frame_occupied_crop_px(frame)
    markers = _embed_fiducial_markers(img, box) if fiducials else []
    cropped = img.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue(), box, markers


def _resolve_local_www_path(hass: HomeAssistant, path: str) -> str:
    """Pure helper: resolves a `/local/...` URL (optionally with a `?query`
    cache-buster, same shape `snapshot_map_as_floorplan` itself returns) back
    to the real file on disk under `config/www/` — the only location this
    integration (and docs/39's own manual-upload flow) ever writes/expects a
    floorplan file. Raises ValueError for anything else (an external URL, an
    absolute filesystem path, an `image_entity` id) naming what IS supported,
    rather than silently guessing."""
    raw = path.split("?", 1)[0]
    if not raw.startswith("/local/"):
        raise ValueError(
            "expected a '/local/...' file path (as returned by "
            "anyvac.snapshot_map_as_floorplan, or a file uploaded under "
            f"config/www/), got: {path!r}"
        )
    rel = raw[len("/local/") :]
    # 1.51.9 (plan po auditu, C1): `/local/../.storage/x` used to resolve to
    # `config/.storage/x`. Resolve symlinks and `..` first and accept only a
    # path that is still inside `config/www/`.
    www = os.path.realpath(hass.config.path("www"))
    target = os.path.realpath(os.path.join(www, *rel.split("/")))
    if os.path.commonpath([www, target]) != www or target == www:
        raise ValueError(f"path must point to a file under config/www/, got: {path!r}")
    return target


LIST_ASSETS_SCHEMA = vol.Schema(
    {vol.Optional("kind", default="furniture"): vol.In(["furniture"])}
)


def _list_furniture_assets(www_dir: str) -> dict[str, Any]:
    """Blocking (executor) — docs/51 §1.4. Lists the image files the studio's
    Furniture tool can place: `config/www/anyvac/furniture/`, non-recursive,
    creating the folder on first use so the user has an obvious place to drop
    files. Only regular files with an image extension, and only ones that
    still resolve inside that folder (a symlink pointing elsewhere is
    skipped, same containment rule as `_resolve_local_www_path`). Raster
    files report their pixel size (Pillow, lazily imported — the card uses it
    to keep the aspect ratio when a piece is added); SVG has none.
    Returned URLs carry no cache-buster: the card stores them as-is in
    `image_base.furniture[].src`, and `mtime` lets it bust the cache itself."""
    folder = os.path.join(www_dir, *FURNITURE_DIR)
    os.makedirs(folder, exist_ok=True)
    root = os.path.realpath(folder)
    assets: list[dict[str, Any]] = []
    for name in sorted(os.listdir(folder), key=str.casefold):
        if not name.lower().endswith(FURNITURE_EXTS) or name.startswith("."):
            continue
        full = os.path.realpath(os.path.join(folder, name))
        if os.path.dirname(full) != root or not os.path.isfile(full):
            continue
        st = os.stat(full)
        item: dict[str, Any] = {
            "name": name,
            "url": "/local/" + "/".join((*FURNITURE_DIR, quote(name))),
            "bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "width": None,
            "height": None,
        }
        if not name.lower().endswith(".svg"):
            try:
                from PIL import Image

                with Image.open(full) as im:
                    item["width"], item["height"] = im.size
            except Exception:  # noqa: BLE001 - an unreadable file is still listed, just without size
                pass
        assets.append(item)
    return {"folder": "www/" + "/".join(FURNITURE_DIR), "assets": assets}


# docs/51 §1.3 — how many mm one floorplan unit is (the floorplan's width = 100
# units), so the studio can offer furniture sizes in centimetres. Geometry stays
# in the backend (docs/14 §1): the card only multiplies by the number returned.
_PT_SCHEMA = vol.Schema({vol.Required("x"): vol.Coerce(float), vol.Required("y"): vol.Coerce(float)})
FLOORPLAN_SCALE_SCHEMA = vol.Schema(
    {
        vol.Optional("crop_box"): vol.Schema(
            {
                vol.Optional("entity"): str,
                vol.Optional("frame_id"): str,
                vol.Required("x0"): vol.Coerce(float),
                vol.Required("y0"): vol.Coerce(float),
                vol.Required("x1"): vol.Coerce(float),
                vol.Required("y1"): vol.Coerce(float),
            }
        ),
        vol.Optional("home_anchors"): [
            vol.Schema({vol.Required("home_px"): _PT_SCHEMA, vol.Required("floor_pct"): _PT_SCHEMA})
        ],
        vol.Optional("home_anchors_frame_id"): str,
        # floorplan width / height — needed to read `floor_pct.y` in units
        vol.Optional("aspect"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=100)),
    }
)


def _similarity_scale(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> float | None:
    """Least-squares scale of the similarity (rotation + uniform scale +
    translation, no mirroring) mapping `src` onto `dst` — Umeyama's closed
    form. None for fewer than 2 distinct points."""
    if len(src) < 2 or len(src) != len(dst):
        return None
    a = np.asarray(src, dtype=float)
    b = np.asarray(dst, dtype=float)
    ac, bc = a - a.mean(axis=0), b - b.mean(axis=0)
    var_a = float((ac**2).sum()) / len(a)
    if var_a < 1e-12:
        return None
    cov = bc.T @ ac / len(a)
    u, d, vt = np.linalg.svd(cov)
    sign = np.diag([1.0, 1.0 if np.linalg.det(u) * np.linalg.det(vt) >= 0 else -1.0])
    scale = float(np.trace(np.diag(d) @ sign)) / var_a
    return scale if scale > 0 else None


def _floorplan_mm_per_unit(
    crop_box: dict[str, Any] | None,
    home_anchors: list[dict[str, Any]] | None,
    aspect: float | None,
    *,
    vacuum_px_per_mm: float | None,
    frame: dict[str, Any] | None,
) -> tuple[float | None, str | None]:
    """Pure: mm per floorplan unit and which input it came from.

    * `crop_box` cut from one vacuum's map (`entity`): the crop's width in that
      map's rendered px over the map's px-per-mm.
    * `crop_box` cut from a home frame (`frame_id`): home px are
      `scale / cell_mm` per mm.
    * `home_anchors` (a drawing calibrated against the home frame, cesta B):
      the least-squares similarity scale from floorplan units to home px,
      then home px to mm. Needs the floorplan's `aspect`.
    """
    if crop_box:
        width_px = float(crop_box["x1"]) - float(crop_box["x0"])
        if width_px > 0:
            if crop_box.get("frame_id") and frame is not None:
                cell_mm = float(frame.get("cell_mm") or 50.0)
                scale = float(frame.get("scale") or 4.0)
                return width_px * cell_mm / scale / 100.0, "crop_frame"
            if crop_box.get("entity") and vacuum_px_per_mm:
                return width_px / vacuum_px_per_mm / 100.0, "crop_vacuum"
    if home_anchors and len(home_anchors) >= 2 and aspect and frame is not None:
        src = [(float(a["floor_pct"]["x"]), float(a["floor_pct"]["y"]) / aspect) for a in home_anchors]
        dst = [(float(a["home_px"]["x"]), float(a["home_px"]["y"])) for a in home_anchors]
        s = _similarity_scale(src, dst)
        if s:
            cell_mm = float(frame.get("cell_mm") or 50.0)
            scale = float(frame.get("scale") or 4.0)
            return s * cell_mm / scale, "home_anchors"
    return None, None


# docs/51 §1.4 — upload a furniture image straight from the studio. The file
# arrives base64-encoded in the service call (furniture images are small);
# written into the same folder `list_assets` lists.
UPLOAD_MAX_BYTES = 2 * 1024 * 1024
UPLOAD_ASSET_SCHEMA = vol.Schema(
    {
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=120)),
        vol.Required("data"): str,
        vol.Optional("kind", default="furniture"): vol.In(["furniture"]),
        vol.Optional("overwrite", default=False): bool,
    }
)


def _safe_asset_name(name: str) -> str:
    """A plain file name in the furniture folder: no folders, no hidden files,
    only letters/digits/`._-` (spaces become `_`), an image extension."""
    base = os.path.basename(name.replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in FURNITURE_EXTS:
        raise ValueError(f"only {', '.join(FURNITURE_EXTS)} files are accepted, got {base!r}")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        raise ValueError(f"not a usable file name: {name!r}")
    return stem[:100] + ext


def _store_asset(www_dir: str, name: str, data_b64: str, overwrite: bool) -> dict[str, Any]:
    """Blocking (executor): validate and write one uploaded image, return the
    same item shape `list_assets` gives. Raster files must open as images
    (Pillow); SVG must look like SVG markup. Never overwrites unless asked —
    a clash gets a `_2`, `_3` … suffix instead."""
    import base64
    import binascii

    filename = _safe_asset_name(name)
    try:
        raw = base64.b64decode(data_b64.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("data is not valid base64") from err
    if not raw:
        raise ValueError("the file is empty")
    if len(raw) > UPLOAD_MAX_BYTES:
        raise ValueError(f"the file is larger than {UPLOAD_MAX_BYTES // 1024} KB")
    width = height = None
    if filename.lower().endswith(".svg"):
        head = raw[:2048].decode("utf-8", "ignore").lower()
        if "<svg" not in head:
            raise ValueError("not an SVG image")
    else:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.verify()
            with Image.open(io.BytesIO(raw)) as im:
                width, height = im.size
        except Exception as err:  # noqa: BLE001 - any decode failure = not an image
            raise ValueError("not a readable image") from err
    folder = os.path.join(www_dir, *FURNITURE_DIR)
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    target = os.path.join(folder, filename)
    n = 2
    while os.path.exists(target) and not overwrite:
        filename = f"{stem}_{n}{ext}"
        target = os.path.join(folder, filename)
        n += 1
    with open(target, "wb") as f:
        f.write(raw)
    st = os.stat(target)
    return {
        "name": filename,
        "url": "/local/" + "/".join((*FURNITURE_DIR, quote(filename))),
        "bytes": st.st_size,
        "mtime": int(st.st_mtime),
        "width": width,
        "height": height,
    }


def _detect_fiducials(image_bytes: bytes, known: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocking (run via executor, same split as `_home_frame_composite_png`)
    — opens `image_bytes` as RGBA, scans it for the fiducial markers
    (`homeframe.find_fiducial_markers`), and pairs whatever it finds against
    `known` (the `{id, home_px}` list `snapshot_map_as_floorplan` returned
    when it embedded them) to produce `home_anchors` pairs in EXACTLY the
    `{home_px, floor_pct}` shape cesta B's `image_base.home_anchors` already
    stores (docs/14 rule 1 — no second anchor format): `floor_pct` is the
    detected pixel centroid normalised by the image's OWN current
    width/height, the identical percentage space every other floorplan-%
    coordinate in this project already uses — so it stays correct no matter
    how the file was cropped/resized/rotated since the snapshot."""
    from PIL import Image

    from .homeframe import find_fiducial_markers

    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGBA")
        width, height = im.size
        rgba = np.array(im)

    detected = find_fiducial_markers(rgba)
    anchors: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in known:
        mid = item["id"]
        px = detected.get(mid)
        if px is None:
            missing.append(mid)
            continue
        x, y = px
        anchors.append(
            {
                "home_px": {"x": item["home_px"]["x"], "y": item["home_px"]["y"]},
                "floor_pct": {"x": x / width * 100.0, "y": y / height * 100.0},
            }
        )
    return {
        "home_anchors": anchors,
        "found": len(anchors),
        "missing": missing,
        "image_width": width,
        "image_height": height,
    }


# ── Guide layer export (docs/37) ───────────────────────────────────────────
# Vivid, non-configurable colours (docs/37 §5) — a tracing aid, not decoration.
_GUIDE_COLOR: dict[str, tuple[int, int, int, int]] = {
    "rooms": (255, 0, 255, 255),  # magenta
    "dry": (0, 255, 0, 255),  # lime
    "wet": (0, 200, 255, 255),  # cyan
}
_GUIDE_FALLBACK_STROKE_PX = 8  # used when calibration (px_per_mm) is unavailable


def _guide_filename(name: str, layer: str) -> str:
    """Pure helper (mirrors `_floorplan_filename`): slugifies `name` for the
    per-layer output filename. Always PNG — the canvas is drawn RGBA, there is
    no source content-type to mirror like the floorplan snapshot has."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "vacuum"
    return f"anyvac_guide_{slug}_{layer}.png"


def _guide_point(p: dict[str, Any], crop: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Pure helper: `px_point − crop origin` (docs/37 §3/§6), or None when the
    point falls outside the crop box — the caller breaks the path there rather
    than drawing a straight line across the gap."""
    x, y = p.get("x"), p.get("y")
    if x is None or y is None:
        return None
    x0, y0, x1, y1 = crop
    if x < x0 or x > x1 or y < y0 or y > y1:
        return None
    return (x - x0, y - y0)


def _guide_path_segments(
    path_px: list[list[dict[str, Any]]], crop: tuple[float, float, float, float]
) -> list[list[tuple[float, float]]]:
    """Pure helper: transforms a list of path segments (each a list of
    `{x, y}` in rendered-image px space, e.g. `path_dry_px`/`path_wet_px`)
    into crop-local canvas coordinates. A point outside the crop breaks the
    segment there (docs/37 §5 "Body mimo crop se přeskakují") instead of
    drawing a straight line to the next in-bounds point; an all-outside or
    empty input segment simply contributes nothing."""
    out: list[list[tuple[float, float]]] = []
    for seg in path_px:
        cur: list[tuple[float, float]] = []
        for p in seg:
            q = _guide_point(p, crop)
            if q is None:
                if cur:
                    out.append(cur)
                    cur = []
                continue
            cur.append(q)
        if cur:
            out.append(cur)
    return out


def _guide_room_rects(
    rooms: list[Any], crop: tuple[float, float, float, float]
) -> list[tuple[tuple[float, float, float, float], str | None]]:
    """Pure helper: each room's `bbox_px` translated into crop-local canvas
    coordinates, paired with its name for the optional label. Rooms without a
    usable bbox are skipped (mirrors `_room_union_bbox_px`); a rect that spills
    slightly past the (padded) crop is left as-is — PIL clips drawing to the
    canvas on its own, no crash, no need to clamp here."""
    x0, y0, _x1, _y1 = crop
    out: list[tuple[tuple[float, float, float, float], str | None]] = []
    for room in rooms:
        bbox = room.get("bbox_px") if isinstance(room, dict) else None
        if not isinstance(bbox, dict):
            continue
        rx0, ry0 = bbox.get("x0"), bbox.get("y0")
        rx1, ry1 = bbox.get("x1"), bbox.get("y1")
        if rx0 is None or ry0 is None or rx1 is None or ry1 is None:
            continue
        name = room.get("name") if isinstance(room, dict) else None
        out.append(((rx0 - x0, ry0 - y0, rx1 - x0, ry1 - y0), name))
    return out


def _guide_room_outline_polygons(
    rooms: list[Any], crop: tuple[float, float, float, float]
) -> list[tuple[list[tuple[float, float]], str | None]]:
    """docs/40 §4.3 (Fáze 2) counterpart of `_guide_room_rects`: each room's
    actual traced shape (`outline_home_px` — a list of `[x, y]` PAIRS, unlike
    `bbox_px`'s `{x0, y0, x1, y1}` dict shape, per `_frame_mm_to_home_px` in
    coordinator.py) translated into crop-local canvas coordinates, paired
    with its name. A room with no outline yet (mask not available — e.g.
    right after a remap) is skipped, same as a room with no bbox is skipped
    by `_guide_room_rects`."""
    x0, y0, _x1, _y1 = crop
    out: list[tuple[list[tuple[float, float]], str | None]] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        outline = room.get("outline_home_px")
        if not outline:
            continue
        pts: list[tuple[float, float]] = []
        ok = True
        for p in outline:
            try:
                px, py = p[0], p[1]
            except (TypeError, IndexError, KeyError):
                ok = False
                break
            if px is None or py is None:
                ok = False
                break
            pts.append((px - x0, py - y0))
        if not ok or len(pts) < 3:
            continue
        out.append((pts, room.get("name")))
    return out


def _image_pixel_size(content: bytes) -> tuple[int, int]:
    """Blocking: opens `content` only to read its (width, height). docs/37 §6
    point 2 — `_padded_crop_box` needs the map image's real pixel size to
    clamp against, and `image_dims` would be a second, independently-drifting
    source of truth for the same number. The decoded image is discarded."""
    from PIL import Image

    with Image.open(io.BytesIO(content)) as im:
        return im.width, im.height


def _guide_font(size: int = 16) -> Any:
    """Best-effort TrueType font for room labels — `ImageFont.load_default()`
    is bitmap and tiny. Labels are nice-to-have (docs/37 §5): any failure here
    just falls back to the default font, never raises."""
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - font lookup is best-effort
            continue
    return ImageFont.load_default()


def _render_guide_layer(
    layer: str,
    canvas_size: tuple[int, int],
    *,
    rects: list[tuple[tuple[float, float, float, float], str | None]] | None = None,
    polygons: list[tuple[list[tuple[float, float]], str | None]] | None = None,
    segments: list[list[tuple[float, float]]] | None = None,
    stroke_px: int = _GUIDE_FALLBACK_STROKE_PX,
    labels: bool = True,
) -> bytes | None:
    """Blocking (run via `hass.async_add_executor_job`, like `_crop_image_to_bbox`).

    Draws ONE guide layer onto a fully transparent RGBA canvas sized exactly to
    `canvas_size` (the crop box's own size — geometry is already crop-local by
    the time it reaches here, produced by `_guide_room_rects`/
    `_guide_room_outline_polygons`/`_guide_path_segments`). Returns PNG
    bytes, or None when nothing was drawn — the caller does not publish a
    layer with no data (docs/37 §4).

    `polygons` (docs/40 §4.3, Fáze 2 — a room's real traced shape) takes
    priority over `rects` (legacy bbox) when both are given, though callers
    only ever pass one of the two. Drawn as closed line loops via
    `draw.line`, not `draw.polygon` — `ImageDraw.polygon`'s `width` support
    for an unfilled outline is Pillow-version-dependent, while `draw.line`
    reliably supports it and is already how `dry`/`wet` paths below are
    drawn, so this is one drawing primitive for every non-filled shape here.
    """
    from PIL import Image, ImageDraw

    w, h = canvas_size
    if w <= 0 or h <= 0:
        return None

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _GUIDE_COLOR.get(layer, (255, 0, 255, 255))
    drew = False

    if layer == "rooms":
        font = _guide_font() if labels else None
        if polygons is not None:
            for pts, name in polygons:
                if len(pts) >= 2:
                    draw.line([*pts, pts[0]], fill=color, width=2, joint="curve")
                    drew = True
                if labels and name:
                    lx, ly = min(p[0] for p in pts), min(p[1] for p in pts)
                    try:
                        draw.text((lx + 4, ly + 4), str(name), fill=color, font=font)
                    except Exception:  # noqa: BLE001 - a label must never sink the export
                        pass
        else:
            for (rx0, ry0, rx1, ry1), name in rects or []:
                draw.rectangle([rx0, ry0, rx1, ry1], outline=color, width=2)
                drew = True
                if labels and name:
                    try:
                        draw.text((rx0 + 4, ry0 + 4), str(name), fill=color, font=font)
                    except Exception:  # noqa: BLE001 - a label must never sink the export
                        pass
    else:  # "dry" / "wet"
        r = max(1, stroke_px // 2)
        for sub in segments or []:
            if len(sub) >= 2:
                draw.line(sub, fill=color, width=stroke_px, joint="curve")
                drew = True
            # PIL's line joints don't round the two free ends of a segment,
            # which reads as a false square corner in the furniture mask
            # (docs/37 §5) — cap both ends (and lone single-point segments)
            # with a filled circle of the same width.
            ends = (sub[0], sub[-1]) if sub else ()
            for px, py in ends:
                draw.ellipse([px - r, py - r, px + r, py + r], fill=color)
                drew = True

    if not drew:
        return None
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _resolve_dock_error(
    status: Any | None, send_raw: Callable[[int], Awaitable[None]]
) -> bool:
    """Confirm a latched dock error, like "Resolved" in the Roborock app
    (docs/47 §3). Returns True when there was one to resolve.

    python-roborock 7.12 has `StatusTrait.resolve_error(code)`; it is called
    with the dock code explicitly (without one it would fall through to the
    robot error). Older libraries: the raw `resolve_error` command, which the
    command enum already carried in 7.4.2.
    """
    err = getattr(status, "dock_error_status", None) if status is not None else None
    try:
        code = int(err) if err is not None else 0
    except (TypeError, ValueError):
        code = 0
    if not code:
        return False
    resolve = getattr(status, "resolve_error", None)
    if callable(resolve):
        await resolve(code)
    else:
        await send_raw(code)
    return True


def _probe_sample(map_trait: Any) -> dict[str, Any]:
    """What one probe fetch saw: map size, robot position, trace lengths."""
    raw = getattr(map_trait, "raw_api_response", None)
    md = getattr(map_trait, "map_data", None)

    def _count(obj: Any) -> int:
        subs = getattr(obj, "path", None) if obj is not None else None
        return sum(len(s) for s in subs) if subs else 0

    pos = getattr(md, "vacuum_position", None) if md is not None else None
    add = getattr(md, "additional_parameters", None) if md is not None else None
    return {
        "raw": bytes(raw) if isinstance(raw, (bytes, bytearray)) else None,
        "x": getattr(pos, "x", None),
        "y": getattr(pos, "y", None),
        "path_points": _count(getattr(md, "path", None)) if md is not None else 0,
        "mop_points": _count(getattr(md, "mop_path", None)) if md is not None else 0,
        "map_sequence": add.get("map_sequence") if isinstance(add, dict) else None,
        "via": getattr(map_trait, "via", None),
    }


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


async def _probe_map_rate(
    map_trait: Any,
    *,
    duration_s: float,
    interval_s: float,
    dynamic_diff: Callable[[], Awaitable[Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout_s: float = 20.0,
    map_every: int = 1,
) -> dict[str, Any]:
    """Fetch the map every `interval_s` for `duration_s` (docs/47 §4).

    `map_every` (docs/48 follow-up): fetch the full map only on every N-th
    sample (0 = never) — to see whether the diff keeps answering without
    full-map requests in between.

    `map_trait` is python-roborock's `MapContentTrait` (`refresh()` sends
    `get_map_v1` and parses it). Reports, per fetch, how long it took and
    whether the map, the robot position and the trace changed — the facts that
    decide whether fetching faster than every 30 s buys anything. With
    `dynamic_diff`, the undocumented `get_dynamic_map_diff` is tried after each
    fetch and only its shape is reported (type, length, first bytes).
    """
    start = clock()
    samples: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    next_at = start
    index = 0
    while True:
        now = clock()
        if now - start > duration_s:
            break
        if now < next_at:
            await sleep(next_at - now)
        t0 = clock()
        row: dict[str, Any] = {"t": round(t0 - start, 1)}
        do_map = map_every > 0 and index % map_every == 0
        index += 1
        if do_map:
            try:
                await asyncio.wait_for(map_trait.refresh(), timeout_s)
            except Exception as err:  # noqa: BLE001 - a failed fetch is a result, not a crash
                row["error"] = f"{type(err).__name__}: {err}"[:200]
            row["latency_ms"] = round((clock() - t0) * 1000)
        else:
            row["map"] = "skipped"
        if do_map and "error" not in row:
            cur = _probe_sample(map_trait)
            raw = cur.pop("raw")
            row["bytes"] = len(raw) if raw is not None else None
            row.update(cur)
            if prev is not None:
                row["changed"] = raw != prev["raw"]
                row["new_path_points"] = cur["path_points"] - prev["path_points"]
                row["new_mop_points"] = cur["mop_points"] - prev["mop_points"]
                if None not in (cur["x"], cur["y"], prev["x"], prev["y"]):
                    row["moved_mm"] = round(math.hypot(cur["x"] - prev["x"], cur["y"] - prev["y"]))
            prev = {**cur, "raw": raw}
        if dynamic_diff is not None:
            d0 = clock()
            try:
                resp = await asyncio.wait_for(dynamic_diff(), timeout_s)
                info: dict[str, Any] = {"type": type(resp).__name__}
                if isinstance(resp, (bytes, bytearray)):
                    info["bytes"] = len(resp)
                    info["head"] = bytes(resp[:32]).hex()
                elif isinstance(resp, (dict, list)) and len(repr(resp)) <= 8000:
                    info["value"] = resp  # whole JSON answer (docs/47 §4 second run)
                else:
                    info["value"] = repr(resp)[:400]
            except Exception as err:  # noqa: BLE001
                info = {"error": f"{type(err).__name__}: {err}"[:200]}
            info["latency_ms"] = round((clock() - d0) * 1000)
            row["dynamic_diff"] = info
        samples.append(row)
        next_at = t0 + interval_s

    ok = [s for s in samples if "error" not in s and s.get("map") != "skipped"]
    changed = [s for s in ok if s.get("changed")]
    change_t = [s["t"] for s in changed]
    return {
        "fetches": len(samples),
        "errors": sum(1 for s in samples if "error" in s),
        "changed": len(changed),
        "latency_ms": _stats([s["latency_ms"] for s in ok]),
        "bytes": _stats([s["bytes"] for s in ok if s.get("bytes") is not None]),
        "seconds_between_changes": _stats([b - a for a, b in zip(change_t, change_t[1:])]),
        "new_path_points_per_change": _stats(
            [s["new_path_points"] + s["new_mop_points"] for s in changed]
        ),
        "moved_mm_per_change": _stats([s["moved_mm"] for s in changed if "moved_mm" in s]),
        "samples": samples,
    }


def _coordinators(hass: HomeAssistant) -> list[Any]:
    """All AnyVac coordinators (in practice one config entry)."""
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = getattr(entry, "runtime_data", None)
        if coord is not None:
            out.append(coord)
    return out


def _active_jobs(hass: HomeAssistant) -> list[JobRunner]:
    return _runner_jobs(hass)


def _cancel_jobs(hass: HomeAssistant, keep_saved: bool = False) -> set[str]:
    """Tear down all running jobs; returns the vacuums that were sent a room.
    ``keep_saved`` (unload / reload): the saved job resumes on the next start."""
    started: set[str] = set()
    for job in list(_active_jobs(hass)):
        started |= job.started_vacuums
        job.finish(keep_saved=keep_saved)
    return started


# Kept under the old name for imports elsewhere (tests, __init__).
_JobRunner = JobRunner


async def _start_job(
    hass: HomeAssistant,
    items: list[dict[str, Any]],
    *,
    mode: str,
    settings: dict[str, Any] | None,
    wash: dict[str, dict[str, Any]],
    context: Context | None,
) -> JobRunner:
    """Cancel any previous job (docs/13 C6: no parallel double-driving) and run."""
    _cancel_jobs(hass)
    runner = JobRunner(hass, items, mode=mode, settings=settings, context=context, wash=wash)
    await runner.start()
    return runner


async def async_restore_job(hass: HomeAssistant, coordinator: Any) -> None:
    """Resume a job saved before a restart (docs/52 §9.5, R3)."""
    if _active_jobs(hass):
        return
    try:
        snap = await coordinator.async_load_job()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("AnyVac: no saved job to resume: %s", err)
        return
    if not snap:
        return
    runner = JobRunner.from_snapshot(hass, snap)
    if runner is None:
        _LOGGER.info("AnyVac: saved job %s is too old to resume; dropped", snap.get("job_id"))
        coordinator.save_job(None)
        return
    _LOGGER.info("AnyVac: resuming job %s after a restart", runner.job_id)
    await runner.start(restored=True)


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


def _home_mm_for(hass: HomeAssistant, duid: str, x_home_px: float, y_home_px: float) -> tuple[int, int]:
    """docs/40 §4.3: home-frame px -> the TARGET robot's own mm, via the
    inverse of its registration (`coordinator.home_px_to_mm`, Fáze 1). Same
    multi-coordinator fan-out as `_mm_for` (a duid belongs to exactly one
    coordinator; the others just answer None)."""
    for coord in _coordinators(hass):
        mm = coord.home_px_to_mm(duid, x_home_px, y_home_px)
        if mm is not None:
            return (round(mm[0]), round(mm[1]))
    raise HomeAssistantError(
        f"anyvac: vacuum '{duid}' has no home-frame registration right now — "
        "cannot convert home-frame pixels to coordinates (check its "
        "'registration' sensor attribute, or use frame: \"robot\" with x_pct/y_pct)"
    )


def _target_mm_for(hass: HomeAssistant, duid: str, call: ServiceCall) -> tuple[int, int]:
    """Resolve one goto/zone_clean corner to the target robot's own mm, from
    EITHER its own rendered-map percent space (`frame: "robot"`, default —
    `x_pct`/`y_pct`) or the shared home frame's px space (`frame: "home"` —
    `x_home_px`/`y_home_px`). The two coordinate styles are both merely
    Optional in the schema (so the unused one can be omitted outright) — this
    is where exactly-one-of is actually enforced, with an error that names
    which fields were expected instead of vol's more generic one."""
    if call.data.get("frame") == "home":
        if "x_home_px" not in call.data or "y_home_px" not in call.data:
            raise HomeAssistantError(
                'anyvac: frame: "home" requires x_home_px and y_home_px'
            )
        return _home_mm_for(hass, duid, call.data["x_home_px"], call.data["y_home_px"])
    if "x_pct" not in call.data or "y_pct" not in call.data:
        raise HomeAssistantError(
            'anyvac: provide x_pct/y_pct (or frame: "home" with x_home_px/y_home_px)'
        )
    return _mm_for(hass, duid, call.data["x_pct"], call.data["y_pct"])


def _target_zone_mm_for(
    hass: HomeAssistant, duid: str, call: ServiceCall
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Same frame: "robot"/"home" choice as `_target_mm_for`, for zone_clean's
    two corners at once (both corners always share one coordinate style —
    there is no reason to mix them in a single call)."""
    if call.data.get("frame") == "home":
        needed = ("x1_home_px", "y1_home_px", "x2_home_px", "y2_home_px")
        if any(k not in call.data for k in needed):
            raise HomeAssistantError(
                'anyvac: frame: "home" requires x1_home_px/y1_home_px/'
                "x2_home_px/y2_home_px"
            )
        a = _home_mm_for(hass, duid, call.data["x1_home_px"], call.data["y1_home_px"])
        b = _home_mm_for(hass, duid, call.data["x2_home_px"], call.data["y2_home_px"])
        return a, b
    needed = ("x1_pct", "y1_pct", "x2_pct", "y2_pct")
    if any(k not in call.data for k in needed):
        raise HomeAssistantError(
            'anyvac: provide x1_pct/y1_pct/x2_pct/y2_pct (or frame: "home" with '
            "x1_home_px/y1_home_px/x2_home_px/y2_home_px)"
        )
    a = _mm_for(hass, duid, call.data["x1_pct"], call.data["y1_pct"])
    b = _mm_for(hass, duid, call.data["x2_pct"], call.data["y2_pct"])
    return a, b


def _require_admin(hass: HomeAssistant, handler: Any) -> Any:
    """Wrap a service handler so only an administrator may call it (C3).

    Same check as Home Assistant's `async_register_admin_service`, done here
    because that helper only gained response support in recent versions. A call
    without a user (automations, scripts, the system) is allowed, as in core."""

    async def _admin_handler(call: ServiceCall) -> Any:
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                raise Unauthorized(context=call.context)
        return await handler(call)

    return _admin_handler


def async_register_services(hass: HomeAssistant) -> None:  # noqa: C901 - one registrar
    """Register the AnyVac services (idempotent)."""

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

    async def _handle_set_floorplan_seat(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.set_floorplan_seat(
                call.data["floorplan"],
                vacuum=call.data.get("vacuum"),
                map=call.data.get("map"),
                image_base=call.data.get("image_base"),
                appearance=call.data.get("appearance"),
                rooms=call.data.get("rooms"),
                room_style=call.data.get("room_style"),
            )

    async def _handle_reset_reach(call: ServiceCall) -> None:
        # docs/51 §2.5 — forget stored reach records; the next complete clean of
        # a room records it again.
        for coord in _coordinators(hass):
            coord.reset_reach(duid=call.data.get("duid"), room=call.data.get("room"))

    async def _handle_reset_learning(call: ServiceCall) -> None:
        for coord in _coordinators(hass):
            coord.reset_learning(
                duid=call.data.get("duid"),
                room=call.data.get("room"),
                kind=call.data.get("kind"),
                estimates=call.data.get("estimates", True),
                baselines=call.data.get("baselines", True),
            )

    def _planner() -> tuple[CleanPlanner, Any]:
        coords = _coordinators(hass)
        if not coords:
            raise HomeAssistantError("anyvac: integration is not set up")
        return CleanPlanner(hass, coords[0]), coords[0]

    async def _read_app_sequences(planner: CleanPlanner, coord: Any, batch: list[str] | None) -> None:
        """A batch robot runs its rooms in the app's order: read it first."""
        for ref in batch or []:
            duid = planner._resolve_duid(str(ref))
            if duid:
                await coord.async_refresh_app_sequence(duid)

    async def _build(call: ServiceCall) -> tuple[list[dict[str, Any]], dict[str, Any], CleanPlanner]:
        planner, coord = _planner()
        await _read_app_sequences(planner, coord, call.data.get("batch"))
        # Stored per-room pins (anyvac.pin_room, docs/18 §7e) are the default;
        # an explicit `pin` parameter on the call wins outright.
        pin = call.data.get("pin") or coord.room_pins or None
        items, plan = planner.build_tasks(
            rooms=[str(r) for r in call.data["rooms"]],
            mode=call.data.get("mode", "dry"),
            vacuums=call.data.get("vacuums"),
            pin=pin,
            settings=call.data.get("settings"),
            batch=call.data.get("batch"),
        )
        return items, plan, planner

    async def _handle_clean(call: ServiceCall) -> None:
        items, plan, planner = await _build(call)
        if not items:
            raise HomeAssistantError(
                "anyvac.clean: no capable vacuum found for the requested rooms/mode "
                f"(plan: {plan})"
            )
        _LOGGER.info("AnyVac clean: %s", {k: plan.get(k) for k in ("dry", "wet", "unassigned", "eta_min", "batch")})
        wash = {it["duid"]: planner.wash_info(it["duid"]) for it in items}
        await _start_job(
            hass, items, mode=call.data.get("mode", "dry"), settings=call.data.get("settings"),
            wash=wash, context=call.context,
        )

    async def _handle_plan(call: ServiceCall) -> dict[str, Any]:
        items, plan, _ = await _build(call)
        return {"plan": plan, "tasks": items}

    async def _handle_edit_job(call: ServiceCall) -> dict[str, Any]:
        """Change the running job (docs/52 §3.4, §9.1). Without a job, `add`
        starts one like `anyvac.clean`."""
        jobs = _active_jobs(hass)
        job = jobs[-1] if jobs else None
        add = [str(r) for r in call.data.get("add") or []]
        remove = [str(r) for r in call.data.get("remove") or []]
        if job is None:
            if not add:
                raise HomeAssistantError("anyvac.edit_job: no job is running")
            planner, coord = _planner()
            items, plan = planner.build_tasks(
                rooms=add, mode=call.data.get("mode") or "dry", pin=call.data.get("pin") or coord.room_pins or None,
                settings=call.data.get("settings"),
            )
            if not items:
                raise HomeAssistantError(f"anyvac.edit_job: no capable vacuum for {add}")
            wash = {it["duid"]: planner.wash_info(it["duid"]) for it in items}
            runner = await _start_job(
                hass, items, mode=call.data.get("mode") or "dry", settings=call.data.get("settings"),
                wash=wash, context=call.context,
            )
            return {"job_id": runner.job_id, "added": [it["id"] for it in items]}
        out: dict[str, Any] = {"job_id": job.job_id, "added": [], "removed": [], "moved": False, "reassigned": []}
        settings = call.data.get("settings") or job.settings
        mode = call.data.get("mode")
        try:
            for room in remove:
                kinds = [None] if not mode or mode == "both" else [mode]
                for kind in kinds:
                    out["removed"] += job.remove_room(room, kind)
            if add:
                planner, coord = _planner()
                busy = {(r, it["kind"]) for it in job.items() if it["state"] not in ("removed",) for r in it.get("rooms") or []}
                # A new room joins the queue of the robot the planner picks,
                # behind what it already has; the wet pass is gated as usual.
                items, _plan = planner.build_tasks(
                    rooms=add, mode=mode or job.mode, pin=call.data.get("pin") or coord.room_pins or None,
                    settings=settings,
                )
                items = [it for it in items if not all((r, it["kind"]) in busy for r in it["rooms"])]
                wash = {it["duid"]: planner.wash_info(it["duid"]) for it in items}
                out["added"] = job.add_items(items, wash)
            mv = call.data.get("move")
            if mv:
                out["moved"] = job.move(mv["room"], mv.get("kind", "dry"), mv["index"])
            for room, kinds in (call.data.get("pin") or {}).items():
                if add and room in add:
                    continue  # already placed by the planner with this pin
                planner, _ = _planner()
                for kind, ref in kinds.items():
                    duid = planner._resolve_duid(str(ref))
                    if not duid or not planner._capable(duid, kind) or not planner._duid_owns_room(duid, room):
                        raise HomeAssistantError(f"anyvac.edit_job: {ref} cannot do the {kind} pass of {room}")
                    item = planner.make_item(duid, kind, [room], settings, item_id=None)  # type: ignore[arg-type]
                    job.reassign(room, kind, item)
                    out["reassigned"].append({"room": room, "kind": kind, "vacuum": item["vacuum"]})
        except ValueError as err:
            raise HomeAssistantError(f"anyvac.edit_job: {err}") from err
        return out

    async def _handle_goto(call: ServiceCall) -> None:
        duid = _resolve_target_duid(hass, call)
        x, y = _target_mm_for(hass, duid, call)
        entity = call.data.get("entity_id") or vacuum_entity_for_duid(hass, duid)
        if not entity:
            raise HomeAssistantError(f"anyvac.goto: no vacuum entity for duid '{duid}'")
        await hass.services.async_call(
            "vacuum",
            "send_command",
            {"entity_id": entity, "command": "app_goto_target", "params": [x, y]},
            blocking=True,
            context=call.context,
        )

    async def _handle_zone_clean(call: ServiceCall) -> None:
        duid = _resolve_target_duid(hass, call)
        (ax, ay), (bx, by) = _target_zone_mm_for(hass, duid, call)
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
            context=call.context,
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
        await hass.services.async_call(
            "vacuum", "send_command", data, blocking=True, context=call.context
        )

    async def _handle_dock_empty(call: ServiceCall) -> None:
        # app_start_collect_dust — documented, confirmed command (docs/26 §3).
        # app_stop_collect_dust is its documented counterpart; HA 2026.9's own
        # `switch.<vacuum>_dust_emptying` sends exactly this pair.
        if call.data.get("action") == "stop":
            await _dock_command(call, "app_stop_collect_dust")
        else:
            await _dock_command(call, "app_start_collect_dust")

    async def _handle_dock_wash(call: ServiceCall) -> None:
        # app_start_wash / app_stop_wash — documented, confirmed commands
        # (docs/26 §3); same pair as HA 2026.9's `switch.<vacuum>_mop_washing`.
        if call.data.get("action") == "stop":
            await _dock_command(call, "app_stop_wash")
        else:
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
        # `{"status": 0}` stops it — the same off command HA 2026.9's
        # `switch.<vacuum>_mop_drying` sends, and the same call this service
        # already made in its own live verification back in 2026-07-24.
        status = 0 if call.data.get("action") == "stop" else 1
        await _dock_command(call, "app_set_dryer_status", {"status": status})

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

    async def _handle_dock_resolve_error(call: ServiceCall) -> None:
        duid = _resolve_target_duid(hass, call)
        coords = _coordinators(hass)
        status = next(
            (s for s in (c.roborock_status_for(duid) for c in coords) if s is not None),
            None,
        )

        async def _send_raw(code: int) -> None:
            await _dock_command(call, "resolve_error", {"error_code": code})

        try:
            resolved = await _resolve_dock_error(status, _send_raw)
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001 - surface any library error to the caller
            raise HomeAssistantError(f"anyvac.dock_resolve_error: {err}") from err
        if resolved:
            # The status trait refreshes itself; the map does not change, so no
            # docs/47 map-change refresh follows — publish the cleared flag now.
            for c in coords:
                await c.async_request_refresh()

    probing: set[str] = set()

    async def _handle_probe_map_rate(call: ServiceCall) -> dict[str, Any]:
        # docs/47 §4: DEBUG/DIAGNOSTIC ONLY. Measures; changes nothing the card
        # or the pipeline reads (the extra fetches only refresh the library's
        # own map trait, which the official integration reads the same way).
        duid = _resolve_target_duid(hass, call)
        rb = next(
            (c for c in (a.roborock_coordinator_for(duid) for a in _coordinators(hass)) if c is not None),
            None,
        )
        props = getattr(rb, "properties_api", None)
        trait = getattr(props, "map_content", None)
        if trait is None:
            raise HomeAssistantError(f"anyvac.probe_map_rate: no Roborock map for vacuum '{duid}'")
        if duid in probing:
            raise HomeAssistantError(f"anyvac.probe_map_rate: already probing '{duid}'")
        transport = call.data["transport"]
        diff = None
        source: Any = trait
        if transport == "local":
            # docs/47 §4: python-roborock fetches maps over the cloud only; this
            # asks the firmware over the local connection and watches both
            # transports for the answer (localprobe.py).
            try:
                v1ch = localprobe.v1_channel_of(rb)
            except RuntimeError as err:
                raise HomeAssistantError(f"anyvac.probe_map_rate: {err}") from err
            if not getattr(v1ch, "is_local_connected", False):
                raise HomeAssistantError(
                    f"anyvac.probe_map_rate: '{duid}' is not connected locally right now"
                )
            source = localprobe.LocalMapSource(
                v1ch, getattr(trait, "converter", None), hass.async_add_executor_job
            )
            if call.data["dynamic_diff"]:

                diff_params = call.data.get("diff_params")

                async def diff() -> Any:
                    r = await localprobe.local_request(
                        v1ch, "get_dynamic_map_diff", params=diff_params
                    )
                    ack = r["ack"]
                    parsed = livediff.parse_diff(ack)
                    d = ack.get("diff") if isinstance(ack, dict) else None
                    if parsed is None or not isinstance(d, dict):
                        # no diff at all (S6 rejects the method, S7 MaxV
                        # answers `{"nonce": 0, "result": 2}`): show it raw
                        return {
                            "answer": repr(ack)[:150], "ack_error": r["ack_error"],
                            "protocols": r["protocols"],
                        }
                    return {  # compact (docs/48 follow-up): what the live poller sees
                        "start": parsed["start"],
                        "points": len(parsed["points"]),
                        "pos": parsed["pos"],
                        "c26": (d.get("26") or {}).get("count"),
                        "c1": (d.get("1") or {}).get("count"),
                        "max_len": (d.get("3") or {}).get("max_len"),
                        "nonzero": sorted(k for k, v in d.items() if isinstance(v, dict) and v.get("count")),
                        "result": ack.get("result"),
                        "protocols": r["protocols"],
                    }

        elif call.data["dynamic_diff"]:
            from roborock.roborock_typing import RoborockCommand  # lazy: library of the official integration

            async def diff() -> Any:
                return await trait.rpc_channel.send_command(RoborockCommand.GET_DYNAMIC_MAP_DIFF)

        device = getattr(rb, "_device", None)
        probing.add(duid)
        try:
            result = await _probe_map_rate(
                source,
                duration_s=call.data["duration_s"],
                interval_s=call.data["interval_s"],
                dynamic_diff=diff,
                map_every=call.data["map_every"],
            )
        finally:
            probing.discard(duid)
        status = getattr(props, "status", None)
        result["vacuum"] = call.data.get("entity_id") or duid
        result["local_connected"] = getattr(device, "is_local_connected", None)
        result["state_at_end"] = getattr(status, "state_name", None)
        result["interval_s"] = call.data["interval_s"]
        result["transport"] = transport
        if transport == "local" and getattr(source, "last", None):
            last = source.last
            result["last_local_answer"] = {
                "map_via": last.get("map_via"),
                "ack": repr(last.get("ack"))[:150],
                "ack_error": last.get("ack_error"),
                "protocols": last.get("protocols"),
            }
        _LOGGER.info(
            "AnyVac: probe_map_rate %s — %d fetches, %d changed, latency %s",
            duid, result["fetches"], result["changed"], result["latency_ms"],
        )
        return result

    async def _handle_snapshot_floorplan(call: ServiceCall) -> dict[str, Any]:
        if call.data.get("frame") == "home":
            frame_id, frame = _select_home_frame(
                hass, call.data.get("frame_id"), service="snapshot_map_as_floorplan"
            )
            content, crop_box, markers = await hass.async_add_executor_job(
                _home_frame_composite_png, frame, call.data.get("fiducials", False)
            )
            filename = _floorplan_filename(call.data.get("name") or "home_frame", "image/png")
            target_dir = hass.config.path("www", "anyvac")
            target_path = os.path.join(target_dir, filename)

            def _write_home() -> None:
                os.makedirs(target_dir, exist_ok=True)
                with open(target_path, "wb") as f:
                    f.write(content)

            try:
                await hass.async_add_executor_job(_write_home)
            except OSError as err:
                raise HomeAssistantError(
                    f"anyvac.snapshot_map_as_floorplan: could not write '{target_path}': {err}"
                ) from err

            url = f"/local/anyvac/{filename}?t={int(time.time())}"
            left, top, right, bottom = crop_box
            _LOGGER.info(
                "AnyVac: snapshotted home frame %s (%d robots) -> %s",
                frame_id, len(frame.get("robots") or {}), target_path,
            )
            result: dict[str, Any] = {
                "path": url,
                "frame": "home",
                "frame_id": frame_id,
                "crop": {"x0": left, "y0": top, "x1": right, "y1": bottom},
            }
            if markers:
                result["fiducials"] = markers
            return result

        if "image_entity" not in call.data:
            raise HomeAssistantError(
                'anyvac.snapshot_map_as_floorplan: provide "image_entity" '
                '(or frame: "home" for a multi-vacuum composite)'
            )
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

        content: bytes = image.content
        content_type: str | None = image.content_type

        # Crop out the unexplored padding around the actual floorplan (docs/30
        # §4a second field follow-up — reported live as "the map is 4:3 and
        # doesn't crop the empty space"). Best-effort: any failure (no rooms
        # known yet, Pillow unavailable, corrupt image) just falls back to the
        # uncropped snapshot rather than failing the whole service call.
        duid = duid_for_entity(hass, entity_id)
        bbox = None
        if duid:
            for coord in _coordinators(hass):
                device = (coord.data or {}).get(duid)
                if device is not None:
                    bbox = _room_union_bbox_px(device.data.get("rooms") or [])
                    break
        crop_box: tuple[int, int, int, int] | None = None
        if bbox is not None:
            try:
                content, content_type, crop_box = await hass.async_add_executor_job(
                    _crop_image_to_bbox, content, bbox
                )
            except Exception as err:  # noqa: BLE001 - crop is a nice-to-have, never fatal
                _LOGGER.warning(
                    "AnyVac: floorplan crop failed for %s, saving uncropped image: %s",
                    entity_id, err,
                )

        filename = _floorplan_filename(
            call.data.get("name") or entity_id.split(".", 1)[-1], content_type
        )
        target_dir = hass.config.path("www", "anyvac")
        target_path = os.path.join(target_dir, filename)

        def _write() -> None:
            os.makedirs(target_dir, exist_ok=True)
            with open(target_path, "wb") as f:
                f.write(content)

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
        result: dict[str, Any] = {"path": url}
        if crop_box is not None:
            # Lets the card place THIS vacuum's own rooms exactly onto the
            # cropped floorplan with no manual dragging (docs/30 §8) — the
            # crop is in the same bbox_px pixel space the card already has
            # from the integration sensor, so it's a plain re-normalisation,
            # no new coordinate system to reconcile.
            left, top, right, bottom = crop_box
            result["crop"] = {"x0": left, "y0": top, "x1": right, "y1": bottom}
        return result

    async def _export_map_guide_home_frame(call: ServiceCall) -> dict[str, Any]:
        """docs/40 §4.3 (Fáze 2): the SAME canvas/layers as the legacy path
        below, but for every vacuum registered into one shared home frame at
        once, and real room shapes (`outline_home_px`) instead of one
        vacuum's `bbox_px` rectangles — a separate function (not another
        `if` branch threaded through the whole legacy body below) because
        every single step differs: no `image_entity`, no single `device`,
        geometry already published in home px so no crop-provenance
        cross-check against a fetched image is needed at all."""
        frame_id, frame = _select_home_frame(hass, call.data.get("frame_id"), service="export_map_guide")
        duids = list((frame.get("robots") or {}).keys())
        devices: list[Any] = []
        for coord in _coordinators(hass):
            for duid in duids:
                d = (coord.data or {}).get(duid)
                if d is not None:
                    devices.append(d)

        explicit_crop = call.data.get("crop")
        if explicit_crop is not None:
            crop_box: tuple[int, int, int, int] = (
                int(explicit_crop["x0"]), int(explicit_crop["y0"]),
                int(explicit_crop["x1"]), int(explicit_crop["y1"]),
            )
        else:
            crop_box = await hass.async_add_executor_job(_home_frame_occupied_crop_px, frame)
        x0, y0, x1, y1 = crop_box
        canvas_size = (x1 - x0, y1 - y0)
        if canvas_size[0] <= 0 or canvas_size[1] <= 0:
            raise HomeAssistantError(
                f"anyvac.export_map_guide: crop box for frame '{frame_id}' is empty"
            )
        crop_tuple = (float(x0), float(y0), float(x1), float(y1))

        stroke_mm = call.data.get("stroke_mm", 300)
        cell_mm = frame.get("cell_mm") or 50.0
        scale = frame.get("scale") or 4.0
        stroke_px = max(1, round(stroke_mm * (scale / cell_mm)))

        requested_layers: list[str] = list(call.data.get("layers") or ["rooms", "dry", "wet"])
        labels = call.data.get("labels", True)
        # docs/51 §2.5 — every robot of the frame on the frame's own canvas.
        reach_home_args: list[Any] = []
        if "reach" in requested_layers:
            for coord in _coordinators(hass):
                for duid in duids:
                    if (coord.data or {}).get(duid) is not None:
                        a = coord.reach_home_args(duid)
                        if a is not None:
                            reach_home_args.append(a)

        # Rooms: dedupe by home_room_id — two robots' masks that IoU-matched
        # into the same physical room (docs/40 §4.2 point 6) must draw ONCE,
        # not twice with (very slightly) different traced outlines.
        seen_room_ids: set[str] = set()
        polygons: list[tuple[list[tuple[float, float]], str | None]] = []
        if "rooms" in requested_layers:
            for device in devices:
                rooms = device.data.get("rooms") or []
                hrid_rooms = [r for r in rooms if isinstance(r, dict) and r.get("home_room_id")]
                other_rooms = [r for r in rooms if not (isinstance(r, dict) and r.get("home_room_id"))]
                fresh = []
                for room in hrid_rooms:
                    hrid = room["home_room_id"]
                    if hrid in seen_room_ids:
                        continue
                    seen_room_ids.add(hrid)
                    fresh.append(room)
                polygons.extend(_guide_room_outline_polygons(fresh + other_rooms, crop_tuple))

        dry_segments: list[list[tuple[float, float]]] = []
        wet_segments: list[list[tuple[float, float]]] = []
        for device in devices:
            if "dry" in requested_layers:
                dry_segments.extend(
                    _guide_path_segments(device.data.get("path_dry_home_px") or [], crop_tuple)
                )
            if "wet" in requested_layers:
                wet_segments.extend(
                    _guide_path_segments(device.data.get("path_wet_home_px") or [], crop_tuple)
                )
        points = {
            "dry": sum(len(s) for s in dry_segments),
            "wet": sum(len(s) for s in wet_segments),
        }

        def _render(layer: str) -> bytes | None:
            if layer == "rooms":
                return _render_guide_layer("rooms", canvas_size, polygons=polygons, labels=labels)
            if layer == "reach":
                layers = [r for a in reach_home_args if (r := reachmod.render_rgba(*a)) is not None]
                if not layers:
                    return None
                h = max(r.shape[0] for r in layers)
                w = max(r.shape[1] for r in layers)
                out = np.zeros((max(h, y1), max(w, x1), 4), dtype=np.uint8)
                for r in layers:
                    view = out[: r.shape[0], : r.shape[1]]
                    hit = r[..., 3] > 0
                    view[hit] = r[hit]
                return reachmod.png_bytes(out, crop_box)
            segs = dry_segments if layer == "dry" else wet_segments
            return _render_guide_layer(layer, canvas_size, segments=segs, stroke_px=stroke_px)

        name = call.data.get("name") or "home_frame"
        target_dir = hass.config.path("www", "anyvac")

        def _write(path: str, data: bytes) -> None:
            os.makedirs(target_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

        paths: dict[str, str] = {}
        ts = int(time.time())
        for layer in requested_layers:
            png = await hass.async_add_executor_job(_render, layer)
            if png is None:
                continue
            filename = _guide_filename(name, layer)
            target_path = os.path.join(target_dir, filename)
            try:
                await hass.async_add_executor_job(_write, target_path, png)
            except OSError as err:
                raise HomeAssistantError(
                    f"anyvac.export_map_guide: could not write '{target_path}': {err}"
                ) from err
            paths[layer] = f"/local/anyvac/{filename}?t={ts}"

        _LOGGER.info(
            "AnyVac: exported home-frame guide layers for frame %s (%d robots) -> %s",
            frame_id, len(devices), sorted(paths),
        )
        return {
            "paths": paths,
            "frame": "home",
            "frame_id": frame_id,
            "crop": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "size": {"w": canvas_size[0], "h": canvas_size[1]},
            "points": points,
        }

    async def _handle_export_map_guide(call: ServiceCall) -> dict[str, Any]:
        if call.data.get("frame") == "home":
            return await _export_map_guide_home_frame(call)

        if "image_entity" not in call.data:
            raise HomeAssistantError(
                'anyvac.export_map_guide: provide "image_entity" '
                '(or frame: "home" for a multi-vacuum export)'
            )
        entity_id = call.data["image_entity"]
        if hass.states.get(entity_id) is None:
            raise HomeAssistantError(
                f"anyvac.export_map_guide: entity '{entity_id}' not found"
            )

        # Same duid/device resolution as snapshot_map_as_floorplan.
        duid = duid_for_entity(hass, entity_id)
        device = None
        coord_for_duid = None
        if duid:
            for coord in _coordinators(hass):
                d = (coord.data or {}).get(duid)
                if d is not None:
                    device = d
                    coord_for_duid = coord
                    break
        rooms: list[Any] = (device.data.get("rooms") or []) if device is not None else []

        # Crop (docs/37 §6 — must not be computed a second, independent way):
        # either the caller's explicit box, or the SAME `_room_union_bbox_px` +
        # `_padded_crop_box` helpers `snapshot_map_as_floorplan` uses, against
        # the SAME image fetched the SAME way (only for its width/height —
        # `_image_pixel_size` — never for its pixels; those are discarded).
        explicit_crop = call.data.get("crop")
        if explicit_crop is not None:
            crop_box: tuple[int, int, int, int] = (
                int(explicit_crop["x0"]), int(explicit_crop["y0"]),
                int(explicit_crop["x1"]), int(explicit_crop["y1"]),
            )
        else:
            bbox = _room_union_bbox_px(rooms)
            if bbox is None:
                raise HomeAssistantError(
                    "anyvac.export_map_guide: no room geometry available yet for "
                    f"'{entity_id}' (needed to compute the crop) — wait for the "
                    "vacuum's next poll, or pass 'crop' explicitly"
                )
            try:
                from homeassistant.components.image import async_get_image

                image = await async_get_image(hass, entity_id, timeout=15)
            except Exception as err:  # noqa: BLE001 - one clear service error
                raise HomeAssistantError(
                    f"anyvac.export_map_guide: could not fetch image from "
                    f"'{entity_id}': {err}"
                ) from err
            try:
                img_w, img_h = await hass.async_add_executor_job(
                    _image_pixel_size, image.content
                )
            except Exception as err:  # noqa: BLE001 - surface as one clear error
                raise HomeAssistantError(
                    f"anyvac.export_map_guide: could not read image dimensions "
                    f"for '{entity_id}': {err}"
                ) from err
            crop_box = _padded_crop_box(bbox, img_w, img_h)

        x0, y0, x1, y1 = crop_box
        canvas_size = (x1 - x0, y1 - y0)
        if canvas_size[0] <= 0 or canvas_size[1] <= 0:
            raise HomeAssistantError(
                f"anyvac.export_map_guide: crop box for '{entity_id}' is empty"
            )

        # Stroke width: robot footprint in mm -> px, via the SAME calibration
        # affine the coordinator already owns (docs/37 §5 — services.py must
        # not solve the affine itself). Falls back to a fixed pixel width so
        # the service never fails just because calibration is unavailable.
        stroke_mm = call.data.get("stroke_mm", 300)
        stroke_px = _GUIDE_FALLBACK_STROKE_PX
        if coord_for_duid is not None and duid:
            ppm = coord_for_duid.px_per_mm(duid)
            if ppm:
                stroke_px = max(1, round(stroke_mm * ppm))

        requested_layers: list[str] = list(call.data.get("layers") or ["rooms", "dry", "wet"])
        labels = call.data.get("labels", True)
        reach_args = (
            coord_for_duid.reach_render_args(duid)
            if coord_for_duid is not None and duid and "reach" in requested_layers
            else None
        )
        crop_tuple = (float(x0), float(y0), float(x1), float(y1))

        dry_segments = (
            _guide_path_segments(device.data.get("path_dry_px") or [], crop_tuple)
            if device is not None and "dry" in requested_layers
            else []
        )
        wet_segments = (
            _guide_path_segments(device.data.get("path_wet_px") or [], crop_tuple)
            if device is not None and "wet" in requested_layers
            else []
        )
        points = {
            "dry": sum(len(s) for s in dry_segments),
            "wet": sum(len(s) for s in wet_segments),
        }

        def _render(layer: str) -> bytes | None:
            if layer == "rooms":
                return _render_guide_layer(
                    "rooms", canvas_size,
                    rects=_guide_room_rects(rooms, crop_tuple), labels=labels,
                )
            if layer == "reach":
                # docs/51 §2.5 — the robot's reach mask on its own map canvas,
                # cut to the SAME crop as every other layer (no second crop path).
                if reach_args is None:
                    return None
                rgba = reachmod.render_rgba(*reach_args)
                return reachmod.png_bytes(rgba, crop_box) if rgba is not None else None
            segs = dry_segments if layer == "dry" else wet_segments
            return _render_guide_layer(layer, canvas_size, segments=segs, stroke_px=stroke_px)

        name = call.data.get("name") or entity_id.split(".", 1)[-1]
        target_dir = hass.config.path("www", "anyvac")

        def _write(path: str, data: bytes) -> None:
            os.makedirs(target_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

        paths: dict[str, str] = {}
        ts = int(time.time())
        for layer in requested_layers:
            png = await hass.async_add_executor_job(_render, layer)
            if png is None:
                continue  # empty layer: no file, no path entry (docs/37 §4)
            filename = _guide_filename(name, layer)
            target_path = os.path.join(target_dir, filename)
            try:
                await hass.async_add_executor_job(_write, target_path, png)
            except OSError as err:
                raise HomeAssistantError(
                    f"anyvac.export_map_guide: could not write '{target_path}': {err}"
                ) from err
            paths[layer] = f"/local/anyvac/{filename}?t={ts}"

        _LOGGER.info("AnyVac: exported guide layers for %s -> %s", entity_id, sorted(paths))
        return {
            "paths": paths,
            "crop": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "size": {"w": canvas_size[0], "h": canvas_size[1]},
            "points": points,
        }

    async def _handle_dump_raw_map(call: ServiceCall) -> dict[str, Any]:
        # docs/40 Fáze 0: DEBUG/DIAGNOSTIC ONLY. Writes the raw Roborock map
        # bytes for one vacuum to disk so the user can pull them into
        # `anyvac/tools/samples/` for the offline home-frame registration probe.
        # Never writes card config, never touches anything the card reads.
        duid = _resolve_target_duid(hass, call)
        raw, meta = _require_raw_map(_coordinators(hass), duid)

        entity = call.data.get("entity_id") or vacuum_entity_for_duid(hass, duid)
        name = call.data.get("name") or (entity.split(".", 1)[-1] if entity else duid)
        filename = _raw_map_filename(name, meta.get("map_flag"))
        # 1.51.9 (plan po auditu, C2): outside `www/`. Everything under `www/`
        # is served at `/local/` without authentication, and a raw map is the
        # floor plan of the user's home.
        target_dir = hass.config.path(DEBUG_DUMP_DIR)
        target_path = os.path.join(target_dir, filename)

        def _write() -> None:
            os.makedirs(target_dir, exist_ok=True)
            with open(target_path, "wb") as f:
                f.write(raw)

        try:
            await hass.async_add_executor_job(_write)
        except OSError as err:
            raise HomeAssistantError(
                f"anyvac.dump_raw_map: could not write '{target_path}': {err}"
            ) from err

        sha1 = hashlib.sha1(raw).hexdigest()[:12]
        _LOGGER.info(
            "AnyVac: dumped raw map for %s -> %s (%d bytes, sha1 %s)",
            duid, target_path, len(raw), sha1,
        )
        return {
            "path": f"{DEBUG_DUMP_DIR}/{filename}",
            "bytes": len(raw),
            "map_index": meta.get("map_index"),
            "map_sequence": meta.get("map_sequence"),
            "sha1": sha1,
        }

    async def _handle_snap_wall_corner(call: ServiceCall) -> dict[str, Any]:
        # docs/40 §5.B: card sends a home-px click, gets back the nearest
        # actual wall-corner vertex from that frame's own `wall_mask` — the
        # heavy lifting is `_snap_wall_corner` below (a pure function, unit
        # tested directly, same split as `_home_frame_composite_png`); this
        # closure only resolves which frame and offloads to the executor.
        frame_id, frame = _select_home_frame(
            hass, call.data.get("frame_id"), service="snap_wall_corner"
        )
        return await hass.async_add_executor_job(
            _snap_wall_corner, frame_id, frame, call.data["x_home_px"], call.data["y_home_px"]
        )

    async def _handle_detect_fiducials(call: ServiceCall) -> dict[str, Any]:
        # docs/40 §5.A.2: `fiducials` here is the exact `{id, home_px}` list
        # `snapshot_map_as_floorplan` returned earlier — the card threads it
        # through unmodified, no re-derivation. Heavy lifting is
        # `_detect_fiducials` (pure, unit tested directly, same split as
        # `_snap_wall_corner`/`_home_frame_composite_png`); this closure only
        # resolves the file path and reads it.
        try:
            target_path = _resolve_local_www_path(hass, call.data["path"])
        except ValueError as err:
            raise HomeAssistantError(f"anyvac.detect_floorplan_fiducials: {err}") from err

        def _read() -> bytes:
            with open(target_path, "rb") as f:
                return f.read()

        try:
            image_bytes = await hass.async_add_executor_job(_read)
        except OSError as err:
            raise HomeAssistantError(
                f"anyvac.detect_floorplan_fiducials: could not read '{target_path}': {err}"
            ) from err

        try:
            result = await hass.async_add_executor_job(
                _detect_fiducials, image_bytes, call.data["fiducials"]
            )
        except Exception as err:  # noqa: BLE001 - surface as one clear service error
            raise HomeAssistantError(
                f"anyvac.detect_floorplan_fiducials: could not read image '{target_path}': {err}"
            ) from err

        if result["found"] == 0:
            raise HomeAssistantError(
                "anyvac.detect_floorplan_fiducials: no fiducial markers found in "
                f"'{target_path}' — the file may have lost its alpha channel (e.g. "
                "re-exported as JPEG, or flattened in an editor), or none of the "
                "marked corners survived the crop"
            )
        return result

    async def _handle_floorplan_scale(call: ServiceCall) -> dict[str, Any]:
        # docs/51 §1.3 — mm per floorplan unit, for the studio's cm fields.
        crop = call.data.get("crop_box")
        anchors = call.data.get("home_anchors")
        ppm: float | None = None
        frame: dict[str, Any] | None = None
        if crop and crop.get("entity"):
            duid = duid_for_entity(hass, crop["entity"])
            for coord in _coordinators(hass):
                if duid and (coord.data or {}).get(duid) is not None:
                    ppm = coord.px_per_mm(duid)
                    break
        frame_id = (crop or {}).get("frame_id") or call.data.get("home_anchors_frame_id")
        if (crop and crop.get("frame_id")) or anchors:
            try:
                _fid, frame = _select_home_frame(hass, frame_id, service="floorplan_scale")
            except HomeAssistantError:
                frame = None
        mm, source = _floorplan_mm_per_unit(
            crop, anchors, call.data.get("aspect"), vacuum_px_per_mm=ppm, frame=frame,
        )
        return {"mm_per_unit": round(mm, 4) if mm else None, "source": source}

    async def _handle_upload_asset(call: ServiceCall) -> dict[str, Any]:
        # docs/51 §1.4 — a furniture image uploaded from the studio.
        try:
            return await hass.async_add_executor_job(
                _store_asset, hass.config.path("www"), call.data["name"], call.data["data"],
                call.data.get("overwrite", False),
            )
        except ValueError as err:
            raise HomeAssistantError(f"anyvac.upload_asset: {err}") from err
        except OSError as err:
            raise HomeAssistantError(f"anyvac.upload_asset: could not write the file: {err}") from err

    async def _handle_list_assets(call: ServiceCall) -> dict[str, Any]:
        # docs/51 §1.4 — the browser cannot list config/www itself.
        try:
            return await hass.async_add_executor_job(
                _list_furniture_assets, hass.config.path("www")
            )
        except OSError as err:
            raise HomeAssistantError(f"anyvac.list_assets: {err}") from err

    async def _handle_cancel(call: ServiceCall) -> None:
        started = _cancel_jobs(hass)
        if call.data.get("return_to_base", True) and started:
            await hass.services.async_call(
                "vacuum",
                "return_to_base",
                {"entity_id": sorted(started)},
                blocking=False,
                context=call.context,
            )

    registrations: list[tuple[str, Any, vol.Schema, SupportsResponse]] = [
        (SERVICE_SELECT_ROOMS, _handle_select_rooms, SELECT_ROOMS_SCHEMA, SupportsResponse.NONE),
        (SERVICE_PIN_ROOM, _handle_pin_room, PIN_ROOM_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SET_LAYERS, _handle_set_layers, SET_LAYERS_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SET_FLOORPLAN_SEAT, _handle_set_floorplan_seat, SET_FLOORPLAN_SEAT_SCHEMA, SupportsResponse.NONE),
        (SERVICE_RESET_LEARNING, _handle_reset_learning, RESET_LEARNING_SCHEMA, SupportsResponse.NONE),
        (SERVICE_CLEAN, _handle_clean, CLEAN_SCHEMA, SupportsResponse.NONE),
        (SERVICE_PLAN, _handle_plan, CLEAN_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_GOTO, _handle_goto, GOTO_SCHEMA, SupportsResponse.NONE),
        (SERVICE_ZONE_CLEAN, _handle_zone_clean, ZONE_CLEAN_SCHEMA, SupportsResponse.NONE),
        (SERVICE_CANCEL, _handle_cancel, CANCEL_SCHEMA, SupportsResponse.NONE),
        (SERVICE_EDIT_JOB, _handle_edit_job, EDIT_JOB_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_DOCK_EMPTY, _handle_dock_empty, DOCK_TOGGLE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_WASH, _handle_dock_wash, DOCK_TOGGLE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_DRY, _handle_dock_dry, DOCK_TOGGLE_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_PUMP, _handle_dock_pump, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_SELF_CLEAN, _handle_dock_self_clean, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_DOCK_RESOLVE_ERROR, _handle_dock_resolve_error, DOCK_ACTION_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SNAPSHOT_FLOORPLAN, _handle_snapshot_floorplan, SNAPSHOT_FLOORPLAN_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_EXPORT_MAP_GUIDE, _handle_export_map_guide, EXPORT_MAP_GUIDE_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_DUMP_RAW_MAP, _handle_dump_raw_map, DUMP_RAW_MAP_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_PROBE_MAP_RATE, _handle_probe_map_rate, PROBE_MAP_RATE_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_SNAP_WALL_CORNER, _handle_snap_wall_corner, SNAP_WALL_CORNER_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_DETECT_FIDUCIALS, _handle_detect_fiducials, DETECT_FIDUCIALS_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_LIST_ASSETS, _handle_list_assets, LIST_ASSETS_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_RESET_REACH, _handle_reset_reach, RESET_REACH_SCHEMA, SupportsResponse.NONE),
        (SERVICE_FLOORPLAN_SCALE, _handle_floorplan_scale, FLOORPLAN_SCALE_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_UPLOAD_ASSET, _handle_upload_asset, UPLOAD_ASSET_SCHEMA, SupportsResponse.ONLY),
    ]
    for name, handler, schema, supports in registrations:
        if name in ADMIN_SERVICES:
            handler = _require_admin(hass, handler)
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(
                DOMAIN, name, handler, schema=schema, supports_response=supports
            )
