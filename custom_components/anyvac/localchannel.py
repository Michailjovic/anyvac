"""Local RPC over the robot's own connection (docs/49 D2).

The one production consumer is the live poller (`coordinator._live_fetch`,
docs/48), which asks the firmware for ``get_dynamic_map_diff`` over the LOCAL
connection every few seconds. `localprobe.py` (a docs/47 measuring tool, DEBUG
ONLY) used to host this code; it now imports it from here.

It reaches into python-roborock internals — and says so, here, instead of in a
debug module's header. What it touches, all of it checked by
`tests/test_localchannel_contract.py` against the installed library:

* the official integration's coordinator: ``_device``;
* the device: ``_channel`` (a ``V1Channel``);
* the V1Channel: ``_local_channel``, ``_mqtt_channel``, ``_security_data`` and the
  public ``is_local_connected``;
* the local channel: ``protocol_version``, ``subscribe``, ``publish``.

A library change makes `v1_channel_of` raise (the live poller then simply stays
off for that robot) — never anything worse; every caller catches.
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
    watch_cloud: bool = True,
    params: Any = None,
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
    # docs/48: the live poller sets watch_cloud=False — subscribing to the
    # cloud topic every few seconds would itself be cloud traffic.
    mqtt = getattr(v1ch, "_mqtt_channel", None) if watch_cloud else None

    request = RequestMessage(method, params=params)
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
