"""Local-only map fetch experiment (docs/47 §4, DEBUG ONLY).

python-roborock fetches maps only through the Roborock cloud: its map RPC
channel is MQTT-only, even when the robot is connected locally. Whether the
FIRMWARE can answer a map request over the local TCP connection is not
documented anywhere. This module asks it directly: it sends ``get_map_v1``
(or any other command) over the local channel and watches BOTH transports for
the answer, so a probe can tell apart

* "map frame came back locally"   -> local map fetching is possible;
* "map frame came back via cloud" -> the firmware always uploads maps to the
  cloud, a local request only triggers it;
* nothing / an error               -> not supported locally.

The transport itself (`v1_channel_of`, `local_request`) lives in `localchannel.py`
since docs/49 D2 — the live poller uses it in production. What stays here is the
measuring tool built on top of it (`LocalMapSource`, the `probe_map_rate` service).
Nothing in this file is used by the pipeline or the card.
"""

from __future__ import annotations

from typing import Any

from .localchannel import LOCAL_TIMEOUT_S, local_request, v1_channel_of  # noqa: F401 - re-exported


class LocalMapSource:
    """Map-trait look-alike for the probe core: `refresh()` fetches the map
    over the local connection only, then exposes `raw_api_response` and the
    parsed `map_data` like python-roborock's `MapContentTrait` does."""

    def __init__(self, v1ch: Any, converter: Any, executor: Any) -> None:
        self._v1ch = v1ch
        self._converter = converter
        self._executor = executor  # async callable(fn, *args) -> result
        self.raw_api_response: bytes | None = None
        self.map_data: Any = None
        self.last: dict[str, Any] | None = None
        self.via: str | None = None  # "local" | "cloud": where the last map frame came from

    async def refresh(self) -> None:
        res = await local_request(self._v1ch, "get_map_v1")
        self.last = res
        if res["map"] is None:
            raise RuntimeError(
                "no map frame (local ack=%r, error=%r, protocols local=%s cloud=%s)"
                % (res["ack"], res["ack_error"], res["protocols"]["local"], res["protocols"]["cloud"])
            )
        # The firmware answers a LOCAL map request by uploading the map to the
        # cloud (measured 2026-10-08, 11/11 fetches). The map is still parsed
        # and measured; each sample says which way it came (`via`).
        self.via = res["map_via"]
        parse = getattr(self._converter, "async_parse_map_content", None)
        if parse is not None:
            content = await parse(res["map"])
        else:
            content = await self._executor(self._converter.parse_map_content, res["map"])
        self.raw_api_response = getattr(content, "raw_api_response", None) or res["map"]
        self.map_data = getattr(content, "map_data", None)
