# Changelog

All notable changes to the AnyVac companion integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
