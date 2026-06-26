# Changelog

All notable changes to the AnyVac companion integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-06-26

### Added

- **Shared room selection (backend-owned).** The set of rooms queued to clean now lives in the
  integration instead of each browser, so phone and PC show the same selection and it can feed the
  orchestrator. Exposed as `selected_rooms` on the map sensor; mutated via the new **`anyvac.select_rooms`**
  service (`{rooms: [...], mode: set | add | remove | toggle | clear}`). Persisted across restarts.

## [0.8.0] - 2026-06-26

### Added

- **Calibration diagnostics + raw path counts on the map sensor.** New attributes: `path_points` /
  `mop_path_points` (the raw, undecimated trajectory point counts, which grow through a clean — a
  better signal than the decimated `path`), and `calib_debug` — the last single-room calibration
  decision (`{confirmed_rooms, clean_type, duration_min, wrote, before, after, reason}`) so it's
  visible why an estimate did or didn't get written.

## [0.7.3] - 2026-06-26

### Fixed

- **Calibration recorded the wrong clean type (dry clean logged as wet).** The clean type was read at
  the finish poll, but the robot resets its settings to its default mode at the end of a clean, so a
  dry clean looked wet by then. The clean type is now captured **while the robot is actually cleaning
  a room** (mid-clean, settings applied) and used for both the estimate and the finish event.

## [0.7.2] - 2026-06-26

### Fixed

- **Version sync / clean re-release.** Aligned the version across `manifest.json`, the README status
  line (was stale at v0.7.0) and the changelog so HACS reports a single consistent version. No code
  change versus 0.7.1 (single-room calibration fix below).

## [0.7.1] - 2026-06-26

### Fixed

- **Single-room calibration now actually fires.** The "is this a single-room clean?" check counted
  every transiently reported room, so a brief boundary cross into a neighbour made it look like a
  multi-room clean and no estimate was written. It now uses the **debounced confirmed** rooms (the
  same signal as `anyvac_room_done`), so a genuine single-room clean calibrates that room's time.

## [0.7.0] - 2026-06-26

### Added

- **`anyvac.run_job` service — the server-side orchestration executor.** The card builds an
  orchestration plan (capability-aware assignment + dry→wet dependencies, since it has the config and
  estimates) and hands it to this service as a list of tasks. The service runs the plan **server-side**
  (survives the dashboard being closed) and **gates each task on `anyvac_room_done` /
  `anyvac_clean_finished`** — so a wet robot is released to follow a dry robot per room without
  colliding. Tasks with no `after` conditions run immediately; a 3 h safety timeout tears down a stuck
  job. Each task may carry pre-clean `selects` (mop mode/intensity) + `fan_speed` before its clean
  `service` call.
- **`duid` exposed on the map sensor** so the card can address `anyvac_room_done` conditions per
  vacuum when building a job.

## [0.6.0] - 2026-06-26

### Added

- **`anyvac_room_done` event — the orchestrator's real-time "room finished" signal.** Fired when a
  vacuum has truly left a room it was cleaning. It does **not** use the raw current room (the robot
  crosses its own room borders mid-clean and briefly reports neighbours): a new room must persist over
  two consecutive polls before it is confirmed, and the previously confirmed room is then reported
  done (`reason: "left"`). The last room is also reported on return-to-dock (`reason: "docked"`).
  Payload: `{ vacuum, duid, room, reason }`. This is the signal a wet robot waits on to follow a dry
  robot per room without colliding.

## [0.5.1] - 2026-06-25

### Fixed

- **Clean-time estimates are now kept per vacuum**, not shared across vacuums. Different models clean
  at different speeds, so a room's learned dry/wet duration is stored under `{duid: {room: {dry, wet}}}`
  and each vacuum's map sensor exposes only its own `rooms_estimate`. (Last-cleaned *freshness* stays
  cross-vacuum by room — that is intentional; only the duration estimates are per-vacuum.)

## [0.5.0] - 2026-06-25

### Added

- **Backend-owned clean-time estimates with single-room calibration.** The integration now learns how
  long each room actually takes, per clean type, and persists it (`{room: {dry, wet}}`, keyed by room
  name across all vacuums). When a cleaning session covers exactly one room, its measured duration
  updates that room+type estimate (first sample = measured, then a rolling average), so estimates
  self-calibrate from real cleans instead of static config guesses. Exposed as `rooms_estimate` on the
  map sensor (and `estimate_dry` / `estimate_wet` per room) for the card to read.
- **Calibration reported on the finish event** — `anyvac_clean_finished` now carries
  `calibrated_room`, `estimate_before` and `estimate_after` when the finished clean was a single-room
  calibration, so a notification can state "estimate for <room> went from X to Y min".

## [0.4.2] - 2026-06-24

### Added

- **`duration_min` on `anyvac_clean_finished`** — the integration times each cleaning session
  (start → end) and reports the elapsed minutes on the finish event, so a fully back-end
  "clean finished" notification can state how long it took without the card being open.

## [0.4.0] - 2026-06-20

### Added

- **Per-room "last cleaned" timestamp sensors** — `sensor.<room>_last_dry` / `_last_wet`
  (`device_class: timestamp`, on an "AnyVac Rooms" device), keyed by room name across vacuums. The
  foundation for "overdue" automations/notifications.
- **Clean events** — `anyvac_clean_started` / `anyvac_clean_finished`
  (`{vacuum, duid, clean_type, rooms}`) fired on cleaning transitions, for clean-finished notifications.
- **Auto-installed notification blueprints** — on setup the integration copies three automation
  blueprints into `config/blueprints/automation/anyvac/` (room overdue, clean finished, vacuum error).
  Create an automation from one, pick your notify service and **write your own message in any
  language** — the integration never composes message text. It never overwrites a blueprint you have
  edited.

## [0.3.1] - 2026-06-19

### Added

- **`mop_path` attribute** — the wet (mop) cleaning path as a separate layer, so the card can draw the
  dry trace (`path`) and the wet trace (`mop_path`) independently.

## [0.3.0] - 2026-06-19

### Changed

- **Room cleaning detected by presence, not `cleaned_rooms`** — on real hardware `cleaned_rooms`
  comes up empty, so tracking now uses `vacuum_room` (where the robot is) while it is cleaning, and
  unions in `cleaned_rooms` when the firmware does provide it.
- **Dry vs wet + cross-vacuum** — history is keyed by room **name** (so it aggregates across all
  vacuums) and records `dry` / `wet` / `any` timestamps. Each room now exposes `last_cleaned`,
  `last_dry`, `last_wet`. Dry/wet is derived from the water mode (best-effort).
- **Raw signals for verification** — the sensor now also exposes `clean_type`, `vacuum_room_name`
  and a `mop_signal` dict (fan power, water mode, mop mode, water-box / carriage state) so the
  dry/wet classification can be confirmed and refined against hardware.

## [0.2.0] - 2026-06-19

### Added

- **Per-room "last cleaned" tracking** (Milestone 3b) — the integration watches
  `MapData.cleaned_rooms` while the vacuum is cleaning and stamps a timestamp per
  room (segment), persisted across restarts. Exposed on the map sensor as the
  `rooms_last_cleaned` attribute and a `last_cleaned` field on each room, plus the
  raw `cleaned_rooms`, `in_cleaning`, `vacuum_room` / `vacuum_room_name` for
  verification. This replaces the fragile helper/automation approach with an
  event-driven, persistent source of truth. (Per-room timestamp sensors + card
  wiring to follow once the `cleaned_rooms` semantics are confirmed on hardware.)
- **No recorder config needed** — the map payload attributes are marked unrecorded
  (`_unrecorded_attributes`), so the large / fast-changing data stays out of the recorder database
  without any `recorder: exclude` in `configuration.yaml`.

## [0.1.1] - 2026-06-19

### Added

- **Room names** — `rooms[].name` is now filled in from the Roborock home trait's
  room mapping (the parsed `MapData.rooms` carries only segment numbers and geometry).

## [0.1.0] - 2026-06-19

Initial release of the companion integration (Milestone 3).

### Added

- **Map data sensor per vacuum** — AnyVac reads the parsed `MapData` (robot
  position, charger, cleaning path, rooms and calibration points) from the
  official Roborock integration's runtime coordinators and exposes it as a sensor
  with the data in its attributes. No second Roborock connection and no manual
  calibration: the pixel↔mm `calibration_points` come straight from the parser.
- **Zero-config setup** — single-step config flow; vacuums are discovered from the
  existing Roborock integration automatically.
- Defensive reads of the Roborock integration's internal structures, so a changed
  upstream layout yields no data rather than an error.
