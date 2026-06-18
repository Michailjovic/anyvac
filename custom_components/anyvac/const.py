"""Constants for the AnyVac companion integration."""

from __future__ import annotations

DOMAIN = "anyvac"
ROBOROCK_DOMAIN = "roborock"

# How often we re-read the parsed map data from the Roborock integration.
SCAN_INTERVAL_SECONDS = 30

# Path can contain thousands of points; decimate before exposing as an attribute
# to keep the entity state (and the recorder) reasonable.
PATH_MAX_POINTS = 400
