"""Constants for the AnyVac companion integration."""

from __future__ import annotations

DOMAIN = "anyvac"
ROBOROCK_DOMAIN = "roborock"

# How often we re-read the parsed map data from the Roborock integration.
SCAN_INTERVAL_SECONDS = 30

# Path can contain thousands of points; simplify before exposing as an attribute
# to keep the websocket payload reasonable (the map attributes are unrecorded —
# recorder DB size is not the concern here, live push size is). Raised from the
# original 400: a real full-apartment dry session (2026-07-22 field data) had
# ~3000 raw points, and 400 meant an ~8x uniform-stride decimation that visibly
# butchered turns — worse the longer a session ran, since this is recomputed
# from the FULL accumulated path every poll (see _decimate/_rdp_simplify in
# coordinator.py, which now preserves shape via Douglas-Peucker instead of
# naive every-Nth-point stride).
PATH_MAX_POINTS = 2000

# Config-entry option (1.1.0): republish the legacy mm-space path arrays
# (`path`, `mop_path`, `path_dry`, `path_wet`) on the map sensor.
#
# The card stopped reading any of them at Fáze 3 of the canon (docs/14) when it
# moved onto the px contract, but the backend kept publishing them — measured at
# ~224 KiB per vacuum per poll of pure websocket payload nobody consumes. They
# are documented in anyvac/README.md as available for user automations, though,
# so this is opt-in rather than a silent removal. Default off; the px attributes
# (`path_dry_px`/`path_wet_px`) and every small mm field are unaffected either
# way, so Pin & Go, zone clean and the card itself do not depend on this.
OPT_EXPOSE_LEGACY_MM = "expose_legacy_mm_attributes"
DEFAULT_EXPOSE_LEGACY_MM = False
