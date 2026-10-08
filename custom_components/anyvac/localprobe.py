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

It reaches into python-roborock internals (`V1Channel._local_channel`,
`_mqtt_channel`, `_security_data`) on purpose — this is a measuring tool, not a
feature, and everything is guarded so a library change only makes the probe
report an error. Nothing here is used by the pipeline or the card.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

LOCAL_TIMEOUT_S = 10.0


def v1_channel_of(roborock_coordinator: Any) -> Any:
    """The python-roborock `V1Channel` behind an official v1 coordinator."""
    device = getattr(roborock_coordinator, "_device", None)
    channel = getattr(device, "_channel", None)
    if channel is None or not hasattr(channel, "_local_channel"):
        raise RuntimeError("python-roborock internals changed: no V1Channel on the device")
    return channel


async def local_request(
    v1ch: Any,
    method: str,
    *,
    timeout_s: float = LOCAL_TIMEOUT_S,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    """Send `method` over the LOCAL connection and report how it was answered.

    Returns ``{"map": bytes | None, "map_via": "local" | "cloud" | None,
    "ack": <rpc result> | None, "ack_error": str | None,
    "protocols": {"local": [...], "cloud": [...]}, "latency_ms": int}``.
    Waits for a map frame (protocol 301) carrying our request id on either
    transport; stops early when the local RPC answer is a real result rather
    than the ``["ok"]`` a map request is acknowledged with.
    """
    from roborock.protocols.v1_protocol import (  # lazy: library of the official integration
        RequestMessage,
        create_map_response_decoder,
        decode_rpc_response,
    )
    from roborock.roborock_message import RoborockMessageProtocol

    local = getattr(v1ch, "_local_channel", None)
    if local is None or not getattr(v1ch, "is_local_connected", False):
        raise RuntimeError("robot is not connected locally")
    security = getattr(v1ch, "_security_data", None)
    mqtt = getattr(v1ch, "_mqtt_channel", None)

    request = RequestMessage(method, params=None)
    message = request.encode_message(
        RoborockMessageProtocol.GENERAL_REQUEST,
        security_data=security,
        version=local.protocol_version,
    )
    map_decoder = create_map_response_decoder(security) if security is not None else None
    done = asyncio.get_running_loop().create_future()
    out: dict[str, Any] = {
        "map": None, "map_via": None, "ack": None, "ack_error": None,
        "protocols": {"local": [], "cloud": []},
    }

    def _finish() -> None:
        if not done.done():
            done.set_result(None)

    def _on(via: str, msg: Any) -> None:
        proto = getattr(msg, "protocol", None)
        out["protocols"][via].append(int(proto) if proto is not None else None)
        if proto == RoborockMessageProtocol.MAP_RESPONSE and map_decoder is not None:
            try:
                resp = map_decoder(msg)
            except Exception as err:  # noqa: BLE001
                out["ack_error"] = f"map frame undecodable: {err}"[:200]
                return
            if resp is not None and resp.request_id == request.request_id:
                out["map"], out["map_via"] = resp.data, via
                _finish()
            return
        if via != "local":
            return
        try:
            resp = decode_rpc_response(msg)
        except Exception:  # noqa: BLE001 - not an RPC answer (status push etc.)
            return
        if resp.request_id != request.request_id:
            return
        if resp.api_error:
            out["ack_error"] = str(resp.api_error)[:200]
            _finish()
            return
        out["ack"] = resp.data
        if resp.data not in (["ok"], "ok"):
            _finish()  # a real result arrived over RPC, nothing else to wait for

    unsubs = [await local.subscribe(lambda m: _on("local", m))]
    if mqtt is not None:
        try:
            unsubs.append(await mqtt.subscribe(lambda m: _on("cloud", m)))
        except Exception:  # noqa: BLE001 - cloud watch is best effort
            pass
    t0 = clock()
    try:
        await local.publish(message)
        try:
            await asyncio.wait_for(done, timeout_s)
        except TimeoutError:
            pass
    finally:
        for unsub in unsubs:
            unsub()
    out["latency_ms"] = round((clock() - t0) * 1000)
    return out


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

    async def refresh(self) -> None:
        res = await local_request(self._v1ch, "get_map_v1")
        self.last = res
        if res["map"] is None:
            raise RuntimeError(
                "no map frame (local ack=%r, error=%r, protocols local=%s cloud=%s)"
                % (res["ack"], res["ack_error"], res["protocols"]["local"], res["protocols"]["cloud"])
            )
        if res["map_via"] != "local":
            raise RuntimeError("map frame arrived via the CLOUD, not locally")
        parse = getattr(self._converter, "async_parse_map_content", None)
        if parse is not None:
            content = await parse(res["map"])
        else:
            content = await self._executor(self._converter.parse_map_content, res["map"])
        self.raw_api_response = getattr(content, "raw_api_response", None) or res["map"]
        self.map_data = getattr(content, "map_data", None)
