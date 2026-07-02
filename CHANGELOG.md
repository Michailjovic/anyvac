# Changelog

All notable changes to the AnyVac companion integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.16.0] - 2026-07-02

Refinements from the first full-apartment day of data.

### Fixed

- **A drive-through no longer stamps history or fires `anyvac_room_done`.** Field
  finding: S6 docks in the Kitchen, so leaving/returning collected ~17 cells there —
  enough to pass the old ≥3-cell floor, stamp Kitchen "dry cleaned" and (worse) fire
  `anyvac_room_done`, which in a whole-home orchestration would release the wet robot
  into an uncleaned room. Stamps and room_done now require real evidence: cells ≥30 %
  of a valid baseline, else ≥10 % of the room bbox (always ≥3). Rooms listed by the
  firmware in `cleaned_rooms` keep the lenient floor.

### Added

- **`anyvac.reset_learning` service** (`{room?, kind?, estimates?, baselines?}`):
  prunes learned clean-time estimates and/or coverage baselines — for entries poisoned
  before the evidence-based typing fix (e.g. wet estimates on a dry-only robot), or
  after furniture changes invalidate a room's baseline.

## [0.15.0] - 2026-07-02

### Fixed

- **Dry/wet is now decided by EVIDENCE, not by the water-mode signal.** Field finding:
  an S7 dry pass (orchestrated, mop intensity set to "off") still reported
  `clean_type: wet` — so its room got a WET history stamp (the dry age never updated on
  the card) and 3 minutes of dry cleaning were learned into the WET estimate table.
  History stamps and calibration now derive the kind from the session's coverage cells:
  wet cells only exist while the mop is physically engaged, dry cells only while suction
  is on (0.14.0), so the cells are ground truth. A room stamps/learns "dry" only with
  dry evidence, "wet" only with wet evidence — and a genuine combined pass correctly
  learns the same minutes into both tables. `calib_debug.rooms` is now per-kind
  (`{room: {active_min, dry: {...}, wet: {...}}}`); `anyvac_clean_finished.calibrated`
  is likewise nested per kind, and the legacy single-room fields were dropped.

## [0.14.0] - 2026-07-02

Field-test fixes from the first 0.13.0 runs + shared layer visibility.

### Fixed

- **A mop-only pass no longer paints the dry layer.** The trajectory of a wet clean with
  suction off (observed: S8, `fan_speed_name: off`) was counted into `path_dry` and dry
  coverage, instantly pushing the dry gauge to 100 % during a wet clean. Dry trace and
  dry coverage now require the robot to actually be VACUUMING (new `vacuuming` attribute
  from `fan_speed_name`; falls back to `clean_type` when unknown). Time attribution still
  uses the full trajectory — movement is movement.
- **Poisoned coverage baselines self-heal.** A baseline learned long ago from a partial
  run (observed: 15 cells vs a 221-cell bbox) made gauges jump to 100 % immediately and
  let the calibration gate pass trivially. Baselines below 20 % of the room's bbox are
  now ignored (gauge falls back to raw % with `~`), and a completed clean covering ≥1.5×
  the stored baseline replaces it outright — upward corrections jump, they don't crawl.

### Added

- **`anyvac.set_layers` service + `view_layers` attribute**: the card's dry/wet layer
  toggles are now backend-shared state — they survive page refreshes and stay in sync
  across browsers/devices, exactly like the room selection. Persisted across restarts.

## [0.13.0] - 2026-07-02

Continuous time calibration (docs/16): every clean calibrates, polling stops mattering.

### Changed

- **Point-weighted time attribution.** Per-room elapsed time is no longer "the whole 30 s
  poll goes to the confirmed room" — each poll's delta is split across rooms in proportion
  to where the NEW trajectory points fell (smallest containing bbox wins on overlaps).
  The trajectory is recorded continuously by the firmware, so accuracy no longer depends
  on the 30 s polling interval, and the old 60 s confirmation debounce no longer eats the
  start of every room (small rooms used to under-measure by up to a third). Polls with no
  new points (paused / stuck) attribute nothing, so pauses fall out automatically.
- **Coverage cells are attributed the same way** (point → its room), so the per-room
  gauges start filling from the first poll instead of waiting ~60 s for room confirmation.
  The room-done debounce remains — but only for the `anyvac_room_done` event.
- **Continuous multi-room calibration.** Every COMPLETED room of every session is now a
  calibration sample — a single-room clean is just the trivial case, not a ritual.
  A room qualifies when the firmware lists it in `cleaned_rooms` or it was confirmed
  during the session, its coverage reaches ≥70 % of the learned full-clean baseline
  (partial cleans are rejected), and its point-weighted active time is 1–180 min.
  `anyvac_clean_finished` gains `calibrated: {room: {before, after}}`; the old
  single-room fields are kept when exactly one room calibrated.
- **`calib_debug` v2**: per-room decisions (`{active_min, cells, baseline, accepted,
  reason, before, after}`) so the Debug tab shows exactly why each room did or didn't
  learn.
- Coverage baselines are now learned only from completed rooms — a drive-through room's
  thin trail of cells can no longer seed a poisoned baseline.

### Fixed

- `manifest.json` version now matches the release (0.12.x shipped stating 0.11.0).

## [0.12.0] - 2026-07-02

Phase 1 of the backend-first canon (docs/14): the integration is now the single tracker
of cleaning sessions — pair with anyvac-card ≥0.37.0, which dropped its client-side copy.

### Fixed

- **Mid-clean mop wash corrupted room tracking (docs/13 A2).** During a mop wash the
  robot sits in the dock's room for minutes while `in_cleaning` stays truthy, so that
  room got "confirmed": the previous room fired `anyvac_room_done` prematurely (releasing
  the wet robot too early), the session gained a second confirmed room (single-room time
  calibration then always failed with "2 confirmed rooms"), and elapsed time accrued to
  the dock's room. Room confirmation, per-room elapsed time and coverage attribution now
  freeze while the raw Roborock state is a transit/self-service state (`washing_the_mop`,
  `going_to_wash_the_mop`, `emptying_the_bin`, `returning_home`, `docking`,
  `going_to_target`, air-drying, charging).
- **`clean_type` is now mop-carriage aware (docs/13 B2).** A configured water level with
  the mop pad physically removed no longer records a dry clean as "wet"
  (`is_water_box_carriage_attached` is honoured when the model reports it).

### Added

- **Segmented dry trace `path_dry` (+ `path_dry_points`, alias `path_wet`).** The parser's
  `path` is the robot's FULL trajectory — transit, mop-wash trips and goto driving
  included — which visually mixed into the "dry" layer and inflated dry coverage during
  wet cleans. `path_dry` contains only points recorded while genuinely cleaning; the
  per-room dry coverage now uses the same filter. `path` / `mop_path` remain unchanged
  for older cards.
- **`status_state` and `transit` attributes** (raw Roborock state + transit flag) for
  debugging and automations.
- **Shared room selection auto-clears** the finished rooms on `anyvac_clean_finished`
  (replaces the card's removed client-side clearing).
- **Pipeline observability (docs/13 B6):** one clear warning when a previously seen
  vacuum stops yielding map data (roborock internals changed / reloading), and an info
  log when data returns — instead of silent debug-level degradation.

### Removed

- Dead `_room_coverage` module function (superseded by per-room cell accumulation long
  ago; it was never called).

## [0.11.0] - 2026-06-27

### Added

- **Normalised coverage (learned full-clean baseline).** Because a room's bounding box includes
  unreachable nooks (arches, under furniture), raw coverage plateaus well below 100 %. The integration
  now learns each room's "full clean" cell count per type (dry/wet) and per vacuum, and normalises
  coverage against it so a fully cleaned room reads ~100 %. It is a rolling average (first sample =
  measured, then weighted 0.4 — the same formula as the time estimate), so it adapts over a few cleans
  when furniture changes; a clean covering less than half the baseline is treated as partial and
  ignored; capped at the bounding-box cell count. `rooms_progress` now also carries `dry_calibrating` /
  `wet_calibrating` (true until the first full clean, while raw bbox % is shown) and the learned
  `dry_baseline` / `wet_baseline`.

### Fixed

- **Time estimate now calibrates from active cleaning time, not wall-clock duration.** A clean that
  was paused (manually or by getting stuck) previously inflated the learned room time. Calibration now
  uses the summed in-cleaning poll time (pauses excluded). `anyvac_clean_finished` / `calib_debug` now
  also report `active_min` alongside `duration_min`.

## [0.10.3] - 2026-06-27

### Changed

- **Per-room coverage split into dry and wet.** `rooms_progress` now reports `dry_pct` / `wet_pct`
  (and `dry_visited` / `wet_visited`) separately — the dry value from the vacuum trace, the wet value
  from the mop trace — so the card can show a dry and a wet gauge side by side. `spatial_pct` is kept
  as the max of the two for backward compatibility.

### Fixed

- **Time no longer accrues to rooms merely driven through.** Per-room elapsed time was attributed to
  the raw current room when no room was confirmed yet, so passing through a room (e.g. the kitchen on
  the way to the target) added time to it. Elapsed is now attributed only to the **confirmed** room,
  matching the coverage logic.

## [0.10.2] - 2026-06-27

### Changed

- **Spatial coverage now counts only the room actively being cleaned.** Previously the whole
  trajectory was bucketed against every room's bounding box each poll, so a room the robot merely
  drives through repeatedly (e.g. crossing the hall on its way to wash the mop) filled up to high
  coverage despite never being cleaned. Coverage is now accumulated incrementally: each poll only the
  newly-added path points are attributed, and only to the room the robot is **confirmed to be cleaning**
  (debounced current room), counting only points inside that room's box. Transit through other rooms
  no longer inflates them — a room accumulates coverage only while it is the active target.

## [0.10.1] - 2026-06-27

### Fixed

- **Spatial coverage was always null.** `_room_coverage` ran inside `_extract_map`, before room names
  are merged in, and it skipped any room without a name — so every room was skipped and coverage came
  out empty. Coverage is now keyed by `segment_id` (always present) and mapped onto room names when
  the progress payload is built, so `spatial_pct` / `visited_cells` / `total_cells` now populate.

## [0.10.0] - 2026-06-27

### Added

- **Per-room cleaning progress (`rooms_progress`) — debug/live signal.** A new map-sensor attribute
  exposing, per room, a **spatial coverage %** (the cleaning path bucketed into ~250 mm grid cells vs
  the room's bounding box) and a **time ratio** (elapsed seconds in the room vs the learned estimate):
  `{room: {spatial_pct, visited_cells, total_cells, time_pct, elapsed_s, est_s}}`. Each room dict also
  carries `progress_pct` (= spatial). Lets the card draw a live per-room gauge to verify tracking
  during a clean. Spatial % is approximate (the bounding box includes furniture, so a fully cleaned
  room plateaus below 100 %).

## [0.9.1] - 2026-06-27

### Fixed

- **"Last cleaned" stamped rooms the robot only drove through.** Per-room history was stamped from the
  raw current room (`vacuum_room`) on every poll, so a room the vacuum merely crossed on its way
  elsewhere (e.g. a large central living room) got a fresh "last cleaned" time even though it was not
  cleaned. Presence-based stamping now uses the **debounced confirmation** (a room must be genuinely
  cleaned and then left — the same signal as `anyvac_room_done`), and the firmware's `cleaned_rooms`
  is still unioned in when present. Pass-throughs are no longer recorded as cleaned.

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
