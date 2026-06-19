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
is the current path-point count and whose attributes carry the map payload:

| Attribute | Meaning |
| --- | --- |
| `vacuum_position` | `{x, y, a}` robot position + angle, in millimetres |
| `charger` | `{x, y}` dock position, in millimetres |
| `calibration_points` | `[{vacuum:{x,y}, map:{x,y}}, …]` — pixel ↔ mm calibration |
| `path` | decimated cleaning path, list of `{x, y}` in millimetres |
| `rooms` | `[{segment_id, name, x0,y0,x1,y1, pos_x,pos_y}, …]` |
| `image_dims` | `{top,left,width,height,scale,rotation}` of the rendered map |

## Recorder

Nothing to configure — the large map attributes (`path`, `rooms`, `calibration_points`, …) are marked
as unrecorded by the integration, so they stay out of your recorder database automatically. No
`recorder: exclude` in `configuration.yaml` is needed.

## Status

Experimental v0.1.0. AnyVac reads the Roborock integration's internal runtime
data; if a future Roborock release changes that structure, AnyVac degrades
gracefully (no data) rather than breaking — please open an issue if that happens.
