# AnyVac (companion integration)

Companion Home Assistant integration for the [AnyVac card](https://github.com/Michailjovic/anyvac-card).

The official Roborock integration already parses the vacuum map into structured
data — robot position, cleaning path, room geometry and calibration points — but
it only renders that to a PNG image entity and never exposes the structured data.
**AnyVac reads that already-parsed data out of the Roborock integration and
re-publishes it**, so the AnyVac card can draw the robot and its path on a custom
floorplan and run zone / pin-and-go cleaning with **no manual calibration**.

## Requirements

- The official **Roborock** integration set up and working (AnyVac reads from it;
  it does not open its own Roborock connection).
- Home Assistant 2024.1.0 or newer.

## Install

1. Add this repository to HACS as a custom repository (category: *Integration*).
2. Install **AnyVac** and restart Home Assistant.
3. Add the **AnyVac** integration (Settings → Devices & services → Add integration).
   There is nothing to configure — it discovers your Roborock vacuums automatically.

## What it exposes

For each Roborock vacuum, a sensor (e.g. `sensor.<vacuum>_anyvac_map`) whose state
is the current path-point count and whose attributes carry the map payload. The card
consumes the **pixel-space** attributes below (`schema_version: 2`) — no calibration
or mm math on the card side:

| Attribute | Meaning |
| --- | --- |
| `schema_version` | `2` once the integration's own 3-point affine solve succeeds; the card shows a degraded-mode banner and disables smart features below this |
| `vacuum_position_px` / `charger_px` | `{x, y}` robot / dock position, in **rendered image pixels** |
| `path_dry_px` | dry-clean trajectory, as **segments** (list of point-lists — one contiguous run per segment, so gaps from transit/mop-wash aren't bridged with a straight line) |
| `path_wet_px` | mop trajectory, flat list of `{x, y}` pixels |
| `rooms` | `[{segment_id, name, bbox_px:{x0,y0,x1,y1}, pos_x, pos_y, estimate_dry, estimate_wet, progress_pct}, …]` |
| `rooms_estimate` | learned per-room clean-time estimates in minutes: `{room: {dry, wet}}` (docs/16 continuous calibration) |
| `rooms_progress` | per-room debug progress: `{room: {spatial_pct, visited_cells, total_cells, time_pct, elapsed_s, est_s}}` |
| `rooms_last_cleaned` | per-room last-cleaned info by clean type (mirrors the timestamp sensors below) |
| `room_sequence` / `room_pins` / `selected_rooms` / `view_layers` | orchestration/UI state the card reads and writes via services below |
| `pipeline_ok` / `pipeline_error` | integration self-diagnostic for the current poll |
| `duid`, `calib_debug`, `transit_cells` | diagnostics — device id, calibration solve debug info, "seen but not counted" cells outside the active job's room scope |

### Home frame (multi-vacuum, `schema_version: 3`)

`schema_version` is now `3` — purely additive, every attribute above is
unchanged. When two or more vacuums share the same physical space, AnyVac
automatically works out how their maps line up (docs/40) and republishes the
SAME geometry above in one shared coordinate space, so a floorplan built once
(via `anyvac.snapshot_map_as_floorplan`) can show every vacuum on it without
any manual "seat" configuration per robot:

| Attribute | Meaning |
| --- | --- |
| `home_frame` | `{id, cell_mm, scale, width_px, height_px}` — the shared raster this vacuum currently belongs to, or `null` if it has none yet (just restarted, or its map failed the decoder self-test) |
| `registration` | `{status, method, rotation_deg, score, iou}` — `status` is `reference` (this vacuum's map founded/still founds the frame), `aligned` (successfully registered onto it), or `unaligned` (its map doesn't currently match anything, e.g. a different floor — it gets its own frame automatically) |
| `vacuum_position_home_px` / `charger_home_px` | the same points as `vacuum_position_px`/`charger_px`, in the shared frame's px space. `vacuum_position_home_px.a` (heading) is its OWN self-consistent convention — standard image-px `atan2` (0°=+x/right, 90°=+y/down), integration ≥ 1.8.1 — distinct from the legacy `vacuum_position_px.a` contract, where a consumer negates `sin` to undo a flip baked into that contract's solved affine |
| `path_dry_home_px` / `path_wet_home_px` | the same segmented trajectories, in the shared frame's px space |
| `rooms[].bbox_home_px` | room bounding box in the shared frame's px space |
| `rooms[].outline_home_px` | the room's actual traced shape (a simplified polygon, ≤ 60 points) instead of just a bounding box |
| `rooms[].home_room_id` | a stable id shared by two vacuums' rooms once their floor masks overlap enough to be the same physical room — use this (not the room name) to match rooms across vacuums |

Every `*_home_px` field and `home_room_id` are `null`/empty for a vacuum with
no home-frame registration (`home_frame: null`) — nothing here changes how a
single-vacuum setup behaves.

`goto`, `zone_clean`, `snapshot_map_as_floorplan` and `export_map_guide`
(below) all accept this shared frame as an alternative to their normal
per-vacuum input (`frame: "home"`, Fáze 2 of docs/40), and `clean`/`plan`
transparently pair up the SAME physical room across two robots that each
call it something different, once a home frame links them. `anyvac-card`
≥ 1.7.0 (Fáze 3 core of docs/40) reads these attributes directly in merged
mode: a home-frame-registered vacuum renders through one shared identity
crop instead of a per-vacuum seat, room rectangles/outlines come from
`bbox_home_px`/`outline_home_px`, and Pin & Go / Zone send `frame: "home"`
automatically — set up once via the editor's "Snapshot home frame as
floorplan" button. A vacuum without a current registration (or an older
card) keeps working exactly as before, per-vacuum.

### Legacy millimetre attributes

The small mm-space fields — `vacuum_position`, `charger`, `calibration_points`
and `rooms[].x0/y0/x1/y1` — are always published, for custom automations that
want to do their own mm math.

The mm **path arrays** (`path`, `mop_path`, `path_dry`, `path_wet`) are **off by
default since 1.1.0**. The card has not read them since it moved to the
pixel-space contract, and they were measured at roughly 224 KB per vacuum on
every 30-second update — pushed over the websocket to every open browser tab
whether anything consumed them or not. If you have automations or templates that
read them, turn them back on under **Settings → Devices & Services → AnyVac →
Configure**; the integration reloads and starts publishing them again.

`path_points` / `mop_path_points` (raw point counts) are published either way.

## Recorder

Nothing to configure — the large map attributes (`path`, `rooms`, `calibration_points`, …) are marked
as unrecorded by the integration, so they stay out of your recorder database automatically. No
`recorder: exclude` in `configuration.yaml` is needed.

## Notifications

AnyVac never writes notification text itself — it exposes **data + events**, and you write the
message (in any language) when you create an automation. Building blocks:

**Per-room timestamp sensors** (on the *AnyVac Rooms* device): `sensor.<room>_last_dry` and
`sensor.<room>_last_wet` (`device_class: timestamp`), keyed by room name across all vacuums. Use them
for "overdue" logic (`now() - states(sensor) > N days`).

**Events:**

| Event | Data |
| --- | --- |
| `anyvac_clean_started` | `{ vacuum, duid, clean_type }` — once per RUN |
| `anyvac_clean_finished` | `{ vacuum, duid, clean_type, rooms, duration_min }` — once per OUTING |
| `anyvac_run_finished` | the same payload, plus `calibrated` (per room and kind: `before`/`after` estimate) — once per RUN, when the whole job is done and its calibration/coverage has been written |
| `anyvac_room_done` | `{ vacuum, duid, room, reason }` — fired when a vacuum has truly left a room it was cleaning (`reason: "left"`, debounced over 2 polls) or on return-to-dock (`reason: "docked"`). The orchestrator's per-room "wet follows dry" signal. |

**Run vs outing** (docs/36): a job dispatched progressively sends the robot out several times, with a
dock trip between batches. `anyvac_clean_finished` fires on every one of those outings — the
orchestrator listens on it to dispatch the next batch — while `anyvac_clean_started` /
`anyvac_run_finished` bracket the whole job. **For a "cleaning done" notification, use
`anyvac_run_finished`**: `clean_finished` would fire once per batch.

All events are fired **server-side** on the vacuum's cleaning transitions, so notifications built on
them fire reliably whether or not the AnyVac card (or any dashboard) is open. `rooms` is the set of
rooms actually visited during the run; `duration_min` is the measured run length in minutes.

**Errors:** use the existing Roborock `sensor.<vacuum>_vacuum_error`.

**Auto-installed blueprints.** On first setup AnyVac copies three automation blueprints into
`config/blueprints/automation/anyvac/`:

- *AnyVac — Room overdue* — pick a room timestamp sensor + threshold days + notify service + message.
- *AnyVac — Clean finished* — fires on `anyvac_clean_finished`; message can use `{{ vacuum }}`,
  `{{ clean_type }}`, `{{ rooms }}`.
- *AnyVac — Vacuum error* — pick the error sensor + notify service + message (`{{ error }}`).

Create an automation from one (Settings → Automations → Blueprints), choose your notify service and
write your own message. Existing (edited) blueprints are never overwritten.

## Services (orchestration)

The integration itself plans and runs cleans server-side (so a job survives the dashboard
closing) — the AnyVac card sends an *intent*, not a pre-built plan:

| Service | What it does |
| --- | --- |
| `anyvac.clean` | Clean intent: `rooms` + `mode` (`dry`/`wet`/`both`) + optional `vacuums` restriction, per-room `pin`, and `settings`. The integration works out capability, room assignment (LPT-balanced), dry→wet gating and per-room pinning, then executes the resulting task list server-side — a wet-capable robot with 2+ rooms dispatches progressively as rooms become ready instead of waiting for all of them (docs/23). Once a home frame links two robots (above), a room name still resolves to whichever robot actually owns that physical room even if that robot calls it something else — no need to repeat the same `clean` call once per robot's own name for it. |
| `anyvac.plan` | Same planner as `anyvac.clean`, response-only — a preview of the assignment and estimated timeline without starting anything. |
| `anyvac.goto` | Pin & go: `x_pct`/`y_pct` (percent of the rendered map image) → the integration converts to real coordinates and sends the robot. With `frame: "home"`, `x_home_px`/`y_home_px` (pixels in the shared home frame) instead. |
| `anyvac.zone_clean` | Zone clean: two corners as percent of the map image, same conversion (or `frame: "home"` + `*_home_px` corners). |
| `anyvac.snapshot_map_as_floorplan` | Saves a map image entity's picture as the shared floorplan file. With `frame: "home"`, renders a composite of every vacuum registered into the shared home frame instead, straight from their aligned floor/wall masks — no single vacuum's `image_entity` involved. |
| `anyvac.export_map_guide` | Draws room-boundary/dry/wet tracing-aid layers for one vacuum's map. With `frame: "home"`, draws every vacuum's rooms and paths on one canvas, with each room's real traced outline (`outline_home_px`) instead of one vacuum's bounding box. |
| `anyvac.cancel` | Stops the running job and (by default) returns started robots to base. |
| `anyvac.select_rooms` / `anyvac.pin_room` / `anyvac.set_layers` / `anyvac.set_room_sequence` / `anyvac.reset_learning` | UI/learning state — room selection, per-room robot pinning, dry/wet layer visibility, the Roborock app's room order (used for ETA), and clearing bad learned estimates. |

A task started by `anyvac.clean` runs once its gating conditions (`anyvac_room_done` per
room, or `anyvac_clean_finished` per vacuum) are met, so a wet robot follows a dry robot per
room without colliding — and a robot never receives a new command while mid-clean (that would
discard the one in progress). `anyvac.run_job` (raw task lists) also exists but is an internal
implementation detail the card no longer builds plans for directly — use `anyvac.clean`.

## Status

Experimental. See `CHANGELOG.md` for version history — the integration's version
syncs to the `anyvac-card` version it was released/tested against (so the number
tells you the minimum compatible card version). AnyVac reads the Roborock
integration's internal runtime data; if a future Roborock release changes that
structure, AnyVac degrades gracefully (no data) rather than breaking — please
open an issue if that happens.
