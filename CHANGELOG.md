# Changelog

All notable changes to the AnyVac companion integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.78.0] - 2026-07-25

Synced to `anyvac-card` 0.78.0 (docs/27 — trace stitching across a job's
sorties).

### Changed

User report: orchestrated whole-home cleans (progressive dispatch, docs/23)
make a wet-capable robot leave the dock multiple times for one job, and the
map never showed the accumulated result — every sortie restart wiped the
dry trace and the wet trace was never persisted at all (it was read live
from the parser's current `mop_path` each poll).

- Dry/wet trace lifetime is now tied to the orchestrated JOB
  (`set_job_rooms` None↔rooms transition, docs/17 §1.3 — set once by
  `_JobRunner.start()`, cleared once by `finish()`) instead of the
  `in_cleaning` session transition. A sortie restart mid-job now closes the
  current trace segment and starts a new one instead of wiping the trace;
  outside an active job (degraded mode, manual per-vacuum start, raw
  `anyvac.run_job`) behaviour is unchanged — a sortie is still the whole
  clean there.
- New `_wet_path`/`_wet_path_open` coordinator state gives the wet (mop)
  trace the same segmented, persisted shape `path_dry` already had — no
  semantic gate needed (the parser's `mop_path` already only contains
  points while the mop is down), a new segment only opens at a job/session
  boundary so a slow mop pass is never fragmented mid-run.
- `path_wet`/`path_wet_px` change shape from a flat point list to a list of
  segments (matches `path_dry`/`path_dry_px`) — card updated in lockstep
  (`anyvac-card` 0.78.0).
- Coverage/calibration state (`_room_cells`, `_transit_cells`,
  `_room_elapsed`, docs/16) is untouched — still resets per sortie exactly
  as before; this change only affects the visual trace's lifetime.
- 2 new tests in `tests/test_coordinator_pipeline.py`: cross-sortie
  stitching under an active job scope, and a regression pojistka proving
  the degraded-mode (no job scope) wipe-on-restart behaviour still holds.
  26/26 tests green.

Spec: `docs/27-slouceni-trasy-napric-vyjezdy.md`.

## [0.76.0] - 2026-07-24

Synced to `anyvac-card` 0.76.0 — two more dock actions (docs/25 §10 second
follow-up).

### Added

- `anyvac.dock_pump` — sends `app_empty_rinse_tank_water` (undocumented
  `RoborockCommand`, drains the mop-rinse basin). Confirmed live against
  the user's S8 MaxV Ultra: matches the manufacturer app's "Pump" action.
- `anyvac.dock_self_clean` — sends `app_amethyst_self_check` (undocumented
  `RoborockCommand`; "amethyst" appears to be Roborock's internal codename
  for the Fill&Drain plumbing accessory). Confirmed live against the
  user's S8 MaxV Ultra: matches the manufacturer app's "Dock and
  Fill&Drain Element Self-Cleaning" action.

Both follow the exact same `_dock_command` plumbing as `dock_empty`/
`dock_wash`/`dock_dry` (`services.py`) — raw `vacuum.send_command`, target
resolution shared with `goto`/`zone_clean`. No new coordinator/sensor
changes. No command found for the app's third Dock Maintenance item
("Fill&Drain Element Drain" — drains the accessory's own built-in sewage
tank); not implemented.

## [0.74.0] - 2026-07-24

Synced to `anyvac-card` 0.74.0 — new dock control feature (docs/25 §7 field
follow-up).

### Added

- `dock_status` attribute on the map sensor: `dust_collection_status`,
  `auto_dust_collection`, `water_shortage_status`, `wash_phase`,
  `wash_ready`, `dock_error_status`, `dock_type` — all read from the SAME
  `properties_api.status` object already fetched every poll for
  `mop_signal` (docs/26 §1 vrstva A, zero new calls/risk). Coarse status
  flags only — Roborock's API doesn't expose fine-grained tank fill
  percentages (verified against python-roborock's `DeviceProp`/
  `DockSummary` dataclasses).
- Three new services: `anyvac.dock_empty` (`app_start_collect_dust`),
  `anyvac.dock_wash` (`app_start_wash`) — both documented, confirmed
  commands (docs/26 §3) — and `anyvac.dock_dry` (`app_set_dryer_status`,
  `{"status": 1}`). `app_set_dryer_status` is present in python-roborock's
  `RoborockCommand` enum but undocumented; no other "start drying now"
  command exists there (`app_get/set_dryer_setting` only configure the
  scheduled duration). Verified live 2026-07-24 against the user's real S8
  MaxV Ultra (start then stop) — both accepted without error, same
  verification method as docs/26 (no response payload either way for
  action/set commands, matching that document's noted limitation). All
  three resolve their target vacuum the same way `anyvac.goto`/
  `anyvac.zone_clean` do (`entity_id` or `duid`) — no coordinates, no map
  math, just a raw dock command.

## [0.67.0] - 2026-07-22

Fixed path trace quality (synced to `anyvac-card` 0.67.0). Found live: a user
report ("looks great during cleaning, gets rewritten to something jagged
after") turned out not to be a rewrite at all — `path_dry`/`path`/`mop_path`
were re-simplified from the FULL accumulated trajectory on every poll with a
fixed, too-small point budget, so the same trace looked progressively worse
as a session ran longer and more raw points piled up against the same cap.

### Fixed

- **Path simplification now shape-preserving.** `_decimate` (coordinator.py)
  used naive every-Nth-point stride sampling, which disproportionately
  destroys turns (where points cluster) while barely touching long straight
  sweeps. Replaced with Ramer-Douglas-Peucker (`_rdp_simplify`, `_RDP_EPSILON_MM
  = 20mm` — below the robot's own positioning precision, so never a visible
  distortion): a point is dropped only if it doesn't meaningfully change the
  line's shape. `max_points` remains a hard safety cap for pathological (very
  long) sessions, now applied as a uniform-stride fallback on the
  already-simplified points rather than the primary algorithm.
- **`PATH_MAX_POINTS` raised 400 → 2000.** The original comment's rationale
  ("keep the recorder reasonable") is stale — these map attributes are
  already `unrecorded`; the only real constraint is live payload size, and a
  few thousand `{x,y}` points is negligible over a 30s poll. Live field data
  (2026-07-22): a real full-apartment dry session had ~3000 raw points against
  the old 400 cap — an ~8x thinning. With both fixes, that same session now
  simplifies to well under 100 points with corners intact (benchmarked: real
  sweep-pattern data 3000 → 31 points in ~8ms; adversarial dense-zigzag worst
  case 6000 → ~1900 points in ~100ms — comfortably inside a 30s poll even
  across 3 vacuums).

### Tests

- New `tests/test_path_decimation.py` (6 tests): a straight run collapses to
  its two endpoints; a sharp corner survives simplification; the hard
  `max_points` cap is still respected even for pathological zigzag input
  where simplification alone barely helps; a realistic two-leg trajectory at
  today's field-observed scale (3000 points) simplifies via RDP (not the
  naive-stride fallback) with the corner intact; already-small inputs pass
  through untouched; `_decimate_segments`'s per-segment independence (no
  merging across an excluded transit/mop-wash gap, docs/14 §3.9) is
  unchanged.

## [0.66.5] - 2026-07-18

Progressive dispatch for multi-room wet passes (docs/23), synced to
`anyvac-card` 0.66.5. Found in the field: an orchestrated "Both" clean on 3
rooms (2 fast dry rooms on one robot, 1 slow on another; a single wet-capable
robot assigned all 3) left the wet robot idle even after both fast rooms'
dry passes were long done — it wouldn't move until the slow room finished
too, because a wet-capable robot's rooms were always one atomic
all-or-nothing task. Dry cleaning needs no such change (confirmed) — this is
wet-only.

### Added

- **Pool tasks** — `CleanPlanner.build_tasks()` now builds a *pool task*
  instead of a static task for a wet-capable robot with 2+ rooms in a
  `both` job: a per-room gate + an `eta_min` timing hint
  (`_estimate_timeline`'s per-room dry-finish estimate, docs/19), no single
  combined `after` list. A robot with just one room, or a standalone `wet`
  job with no dry pass to gate against at all, keeps the existing static
  task unchanged — nothing to stagger there.
- **Adaptive threshold batching** — `_JobRunner._dispatch_pools()`: when a
  pool task has some rooms with a genuinely met gate (real
  `anyvac_room_done` event, never an estimate alone) and the robot is free,
  it checks whether another of its rooms is estimated ready "soon"
  (`BATCH_WAIT_THRESHOLD_MIN` = 3 min). If so it waits (one recheck
  scheduled via `async_call_later`) to fold both into one dock trip —
  naturally reproducing the old "one big batch for the whole apartment"
  behavior when rooms finish close together. If not, it dispatches
  immediately with whatever's ready. A hard cap (`BATCH_WAIT_CAP_MIN` = 6
  min, measured from the first moment something was ready) forces a
  dispatch even if a promised "soon" room never actually arrives — a bad
  estimate costs at most one extra dock trip, never a wrong/early command.
  Dispatch itself is always gated by the real event, the estimate is only a
  wait-vs-go hint. A second batch for the same robot can only go out after
  its `anyvac_clean_finished` for the first — sending a new
  `app_segment_clean` mid-clean discards whatever the robot is doing
  (confirmed hard constraint, docs/23 §2).
- Neither constant is learned yet (unlike per-room time estimates, docs/16)
  — fixed starting points, revisit from field experience.

### Tests

- New `tests/test_planner_pool_tasks.py` (4 tests): multi-room wet robot
  gets a pool task with correct per-room gates/segments/eta hints;
  single-room and standalone-`wet`-mode robots keep the static task
  (regression guard); a both-capable robot's pool task carries its own
  dry-session gate.
- New `tests/test_job_runner_pool.py` (4 tests): two close-together rooms
  batch into one dispatch; a fast+slow pair splits into two dispatches with
  the second withheld until the robot's own `anyvac_clean_finished`; a bad
  estimate is forced out by the wait cap (fires the actual scheduled
  recheck callback, not just the decision math); a plain static task (no
  `pool` key) bypasses the pool machinery entirely and dispatches
  immediately as before.

## [0.51.0] - 2026-07-16

Fáze 4 kánonu (docs/14), part 1: pytest coverage for the coordinator's core
pipeline, plus plan-scope transit labeling (docs/17 §1.3). No card change
required — pairs with `anyvac-card` 0.65.0.

### Added

- **Plan-scope transit labeling** — `AnyVacCoordinator.set_job_rooms(duid,
  rooms)` records the room-name scope of the currently-running orchestrated
  job per vacuum; `_attribute_points` now reroutes any point landing outside
  that scope into a new `_transit_cells` bucket instead of `_room_cells`,
  even when the vacuum's raw state is NOT a `TRANSIT_STATE` (a robot
  genuinely "cleaning" per firmware state while driving through an
  unselected room on the way to a selected one — the one case the existing
  state-based transit gate cannot see). Stored for debug visibility only
  ("ukládat, ale nepočítat"): never feeds calibration, coverage, or elapsed
  time. Exposed on the map sensor as `transit_cells`
  (`{room_name: cell_count}` or `null`). Wired from `services.py`'s
  `_JobRunner`, which applies the scope (union of a vacuum's dry + wet
  assignment from the plan) on job start and clears it on finish/cancel —
  `anyvac.run_job`'s raw task lists deliberately do NOT carry this (docs/17
  explicitly rules out bolting it on there separately).

### Tests

- New `tests/test_coordinator_pipeline.py` (5 tests): a mid-clean mop wash
  freezing coverage/elapsed-time attribution and room-done detection without
  losing the still-genuinely-cleaning time either side of it; a drive-through
  room correctly rejected at calibration ("not completed (transit only?)")
  despite having attributed cells; a single session calibrating multiple
  rooms independently (docs/16 continuous calibration); plan-scope transit
  labeling catching a drive-through the state-only gate misses; and two
  independently-owned vacuums sharing one coordinator without their per-duid
  learned estimates/sessions cross-contaminating (while documenting that
  `_history`'s cross-vacuum-by-room-name design is a deliberate, unchanged
  tradeoff for a genuine two-household name collision).

## [0.50.0] - 2026-07-15

Sequence-aware orchestration (docs/19). Ships in lockstep with `anyvac-card`
0.50.0 (the consolidated meta bar + landscape cockpit rebuild).

### Added

- **`room_sequence` coordinator state** — `{room_name: 1-based position}`,
  mirroring the room order configured in the Roborock app. Field-confirmed:
  the app's configured sequence is always dominant regardless of what order
  HA sends segment ids in, so this is recorded as ground truth rather than
  something the backend tries to control. Persisted across restarts and
  shared across dashboards, following the same pattern as `room_pins` /
  `view_layers`.
- **`anyvac.set_room_sequence`** — `{rooms: [key, ...]}`: replaces the whole
  sequence; position in the list = the sequence number. The card's new Maps
  tab reorder list calls this on every drag-drop.
- **`room_sequence` sensor attribute** — published on every AnyVac map
  sensor (excluded from the recorder), read by the card's editor.
- **Sequence-aware ETA in `anyvac.plan` / `anyvac.clean`** (`CleanPlanner._estimate_timeline`) —
  per-robot cumulative dry time is now computed in the app's configured
  sequence order (falling back to the end of the queue, flagged in the new
  `unsequenced` plan field, for any room without a configured position), and
  each room's wet pass is gated on that specific room's dry-finish time
  rather than waiting for the whole dry batch. Response now includes
  `eta_min`, `timeline` (per-room dry/wet finish times), and `unsequenced`.
- **`tests/test_planner_timeline.py`** — first pytest suite for this
  integration (5 tests covering single/multi-robot timelines, missing
  sequence fallback, empty plans, and the no-estimate-yet default).

## [0.19.1] - 2026-07-15

### Fixed

- **Finished dry trace looked like a scribble** — `path_dry`/`path_dry_px` was a
  single flat point list. Excluding transit/mop-wash points (docs/14 §3.9) leaves
  gaps in the trajectory; the card drew it as one `<polyline>`, so the point right
  before a gap got joined to the point right after it with a straight line. A live,
  still-single-room trace never hit a gap and looked correct; a finished multi-room
  session had one spurious diagonal line per room transition / mop-wash trip,
  reading as chaos next to the clean in-progress trace. `path_dry`/`path_dry_px`
  are now a **list of contiguous segments** — a new segment starts whenever the
  dry gate (`cleaning and not transit and vacuuming`) closes and reopens — so a
  gap is never bridged. `path_dry_points` now sums across segments.

## [0.19.0] - 2026-07-11

Phase B (backend part) of the responsive rebuild (docs/18 §7e): shared per-room
vacuum pins.

### Added

- **`anyvac.pin_room`** — `{room, vacuum?}`: pin a room to a specific vacuum
  (entity_id or duid); omit `vacuum` to unpin. Pins are stored on the coordinator
  (persisted across restarts) and shared across devices/browsers like the room
  selection.
- **`room_pins` sensor attribute** — the current pins, published on every AnyVac
  map sensor next to `selected_rooms` (excluded from the recorder).
- **Planner default pins:** `anyvac.clean` / `anyvac.plan` use the stored pins when
  the call has no explicit `pin` parameter (an explicit `pin` wins outright). The
  existing pin semantics apply — a pin only holds when the pinned robot knows the
  room and is capable of the pass kind, otherwise assignment falls back with a
  warning.
- **One-shot lifecycle:** a room's pin is auto-cleared when that room's clean
  finishes (same lifecycle as the selection auto-clear).

## [0.18.0] - 2026-07-04

Fáze 2 of the backend-first canon (docs/14 §5): contract v2 on the backend. The card
is intentionally unchanged — v1 (mm) attributes are published in parallel until the
Fáze 3 frontend switch.

### Added

- **px-space geometry (docs/14 §3.6):** the sensor now additionally publishes
  `schema_version: 2`, `vacuum_position_px`, `charger_px`, `path_dry_px`,
  `path_wet_px` and `rooms[].bbox_px` — all in rendered-map-image pixels, computed
  from the parser's calibration points via an exact 3-point affine solve (validated
  against live S6 data with an exact round-trip). mm never needs to leave the
  backend again; the mm attributes remain for the v1 card.
- **`anyvac.clean`** — clean INTENT service: `rooms` + `mode` (dry/wet/both),
  optional `vacuums` restriction (flat list or `{dry: [...], wet: [...]}`), per-room
  pinning `pin: {room: vacuum}` (overrides the planner when the pinned robot knows
  the room and is capable), and `settings: {dry/wet: {fan_speed, mop_mode,
  mop_intensity, repeat}}`. The backend plans server-side: intrinsic capability
  detection (wet = electronic water box present), LPT assignment weighted by the
  learned per-vacuum estimates, segment resolution via `segment_id`, dry→wet gating
  on per-room `anyvac_room_done`, dry passes force the mop intensity off, repeat
  goes to the firmware inside `app_segment_clean` (no dock-restart hacks, §3.8).
  Mirrors the field-proven card planner 1:1, executed by the same job runner.
- **`anyvac.plan`** — response-only preview: returns the planned assignment
  (`{dry: {vacuum: rooms}, wet: {...}, unassigned}`) and the task list without
  executing anything.
- **`anyvac.goto` / `anyvac.zone_clean`** — pin & go and zone cleaning with
  coordinates as PERCENT of the map image (`x_pct`/`y_pct`); pct→px→mm conversion
  happens in the backend (inverse of the calibration affine). Target vacuum by
  `entity_id` or `duid` (resolved through the device registry).
- **`anyvac.cancel`** — tears down all running jobs and (by default) sends the
  robots whose tasks were already dispatched back to the dock. Starting a new
  `anyvac.clean`/`run_job` now also cancels the previous job first (docs/13 C6 —
  no more parallel jobs double-driving the same robots).
- **`pipeline_ok` + `pipeline_error` attributes** (docs/13 B6): the piggyback
  pipeline health is now visible on every sensor, not only in the log.

### Changed

- `anyvac.run_job` is now an INTERNAL executor under `anyvac.clean` (docs/14 §5);
  it stays registered for the v1 card transition and disappears from docs in Fáze 3.
- Pre-clean setting calls (mop selects, fan speed) are best-effort: an unknown
  select option logs a warning instead of aborting the clean command.
- Vacuum entity / mop select entities are resolved server-side from the device &
  entity registries (no card-provided entity plumbing needed in v2).

## [0.17.3] - 2026-07-04

### Fixed

- **Stale live data after docking:** `rooms_progress` kept showing residual
  percentages collected during the drive home (e.g. "Kitchen 9 %" on S6 after the
  2026-07-03 validation run) until the next session started. The per-room session
  accumulators are now cleared right after session-end calibration and baseline
  learning consume them.
- **`transit` / `vacuuming` flags are session-gated:** a docked robot reported
  `transit: true` (charging is a transit state) and `vacuuming: true` (last fan
  speed still set) forever after a clean. Both now read `false` whenever
  `in_cleaning` is false; all internal consumers already conditioned on it, so
  this is purely an observability fix.

## [0.17.2] - 2026-07-03

### Added

- **`debug_map.watermarks`** — goto/predicted navigation paths are transient (cleared
  once the robot arrives), so a manual post-clean dump always shows 0 (confirmed in the
  field). The coordinator now remembers the last non-empty sighting per vacuum
  (point count, timestamp, robot state, sample), so one regular clean definitively
  answers whether the firmware publishes navigation routes usable for transit labeling.
- **`debug_map.carpet_map_sample`** — coordinates of the cells the robot classifies as
  carpet (ultrasonic models; S7 reported 8 in a carpet-free flat, so these are false
  positives — mats, thresholds or glossy tiles — and the positions will identify the
  culprit).

### Field findings (2026-07-03, full-apartment day — no code impact)

- Continuous multi-room calibration validated: one 53-min 4-room dry session calibrated
  3 rooms; transit-only rooms rejected. Estimates converged to the observed times
  (23:09/23:00). Evidence-based typing kept wet timestamps intact through dry cleans;
  the dock-trip gate correctly withheld the Kitchen stamp and room_done during an S8
  wash trip.
- `map_data_fields` inventory identical across S6/S7/S8; `cleaned_areas` does NOT exist
  as a vector field (the "Cleaned area" drawable renders from image pixels);
  `image.data` is a full image object + `additional_layers` — room pixel MASKS are
  feasible via the parser's deterministic segment palette (docs/17 §3). S7 reports
  `carpet_map_count: 8`.

## [0.17.1] - 2026-07-02

### Added

- **`debug_map` probe extended** after cross-checking the roborock integration's map
  Options dialog: `cleaned_areas`, `no_carpet_areas`, `goto` target, and full
  position/type lists for all four obstacle variants (`obstacles`,
  `obstacles_with_photo`, `ignored_obstacles`, `ignored_obstacles_with_photo`).
  Most importantly, `map_data_fields` and `image_fields` list every public field the
  parser actually populates on the given model — the definitive reverse-engineering
  inventory, no more guessing which data sources exist.

## [0.17.0] - 2026-07-02

### Added

- **`debug_map` attribute — reverse-engineering probe of unadopted MapData fields**
  (docs/17). Exposes per vacuum: `goto_path` / `predicted_path` (counts + decimated
  sample — candidates for exact transit labeling of mop-wash trips), `walls` (NOTE:
  user-drawn VIRTUAL walls, not physical structure), `no_go_areas`,
  `no_mopping_areas`, `zones` (candidates for subtracting blocked area from room
  coverage totals), `obstacles` (positions + type), and presence/size probes for
  `blocks`, `carpet_map`, ignored obstacles and the image pixel layer. Visible in the
  card editor's Debug tab under Raw attributes; excluded from the recorder. Field data
  from a regular clean (ideally with a mid-clean mop wash) will decide which of these
  sources get adopted for transit labeling and analytics v2.

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
