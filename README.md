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
| `anyvac_clean_started` | `{ vacuum, duid, clean_type }` |
| `anyvac_clean_finished` | `{ vacuum, duid, clean_type, rooms, duration_min }` plus `calibrated_room, estimate_before, estimate_after` when the session was a single-room calibration |
| `anyvac_room_done` | `{ vacuum, duid, room, reason }` — fired when a vacuum has truly left a room it was cleaning (`reason: "left"`, debounced over 2 polls) or on return-to-dock (`reason: "docked"`). The orchestrator's per-room "wet follows dry" signal. |

Both events are fired **server-side** on the vacuum's cleaning transitions, so notifications built on
them fire reliably whether or not the AnyVac card (or any dashboard) is open. `rooms` is the set of
rooms actually visited during the session; `duration_min` is the measured session length in minutes.

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
| `anyvac.clean` | Clean intent: `rooms` + `mode` (`dry`/`wet`/`both`) + optional `vacuums` restriction, per-room `pin`, and `settings`. The integration works out capability, room assignment (LPT-balanced), dry→wet gating and per-room pinning, then executes the resulting task list server-side — a wet-capable robot with 2+ rooms dispatches progressively as rooms become ready instead of waiting for all of them (docs/23). |
| `anyvac.plan` | Same planner as `anyvac.clean`, response-only — a preview of the assignment and estimated timeline without starting anything. |
| `anyvac.goto` | Pin & go: `x_pct`/`y_pct` (percent of the rendered map image) → the integration converts to real coordinates and sends the robot. |
| `anyvac.zone_clean` | Zone clean: two corners as percent of the map image, same conversion. |
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
