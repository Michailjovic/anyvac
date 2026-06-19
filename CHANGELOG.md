# Changelog

All notable changes to the AnyVac companion integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
