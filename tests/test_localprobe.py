"""Local-only map fetch experiment (docs/47 §4) — `localprobe.local_request`
with fake transports. The python-roborock decoders are replaced by fakes; the
real `RequestMessage` encodes the request whose id the fakes echo back."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
import roborock.protocols.v1_protocol as v1p
from roborock.roborock_message import RoborockMessageProtocol

from custom_components.anyvac import localprobe

MAP = RoborockMessageProtocol.MAP_RESPONSE
RPC = RoborockMessageProtocol.RPC_RESPONSE


class _Chan:
    def __init__(self) -> None:
        self.cbs: list[Any] = []
        self.protocol_version = v1p.LocalProtocolVersion.V1

    async def subscribe(self, cb: Any) -> Any:
        self.cbs.append(cb)
        return lambda: self.cbs.remove(cb)

    def emit(self, msg: Any) -> None:
        for cb in list(self.cbs):
            cb(msg)


class _V1:
    """Fake V1Channel: publishing on the local channel triggers `script`."""

    def __init__(self, script: Any, local_ok: bool = True) -> None:
        self._local_channel = _Chan()
        self._mqtt_channel = _Chan()
        self._security_data = SimpleNamespace(to_dict=lambda: {"endpoint": "e", "nonce": "n"})
        self.is_local_connected = local_ok
        self.script = script
        self.sent_ids: list[int] = []

        async def publish(msg: Any) -> None:
            rid = json.loads(json.loads(msg.payload)["dps"]["101"])["id"]
            self.sent_ids.append(rid)
            asyncio.get_running_loop().call_soon(self.script, self, rid)

        self._local_channel.publish = publish


@pytest.fixture(autouse=True)
def _fake_decoders(monkeypatch: pytest.MonkeyPatch) -> None:
    def map_dec_factory(_sec: Any) -> Any:
        return lambda m: SimpleNamespace(request_id=m.rid, data=m.data)

    def rpc_dec(m: Any) -> Any:
        if m.protocol != RPC:
            raise ValueError("not rpc")
        return SimpleNamespace(request_id=m.rid, api_error=None, data=m.data)

    monkeypatch.setattr(v1p, "create_map_response_decoder", map_dec_factory)
    monkeypatch.setattr(v1p, "decode_rpc_response", rpc_dec)


def _msg(proto: Any, rid: int, data: Any) -> Any:
    return SimpleNamespace(protocol=proto, rid=rid, data=data)


@pytest.mark.asyncio
async def test_map_answered_locally() -> None:
    def script(v: _V1, rid: int) -> None:
        v._local_channel.emit(_msg(RPC, rid, ["ok"]))
        v._local_channel.emit(_msg(MAP, rid, b"MAPBYTES"))

    r = await localprobe.local_request(_V1(script), "get_map_v1", timeout_s=1)
    assert r["map"] == b"MAPBYTES" and r["map_via"] == "local"
    assert r["ack"] == ["ok"]


@pytest.mark.asyncio
async def test_map_answered_via_cloud_is_flagged() -> None:
    def script(v: _V1, rid: int) -> None:
        v._local_channel.emit(_msg(RPC, rid, ["ok"]))
        v._mqtt_channel.emit(_msg(MAP, rid, b"MAPBYTES"))

    v = _V1(script)
    r = await localprobe.local_request(v, "get_map_v1", timeout_s=1)
    assert r["map_via"] == "cloud"

    class _Conv:
        async def async_parse_map_content(self, data: bytes) -> Any:
            return SimpleNamespace(raw_api_response=data, map_data="parsed")

    src = localprobe.LocalMapSource(v, _Conv(), executor=None)
    await src.refresh()  # measured anyway, flagged
    assert src.via == "cloud" and src.map_data == "parsed"


@pytest.mark.asyncio
async def test_frames_for_other_requests_are_ignored_and_timeout_reported() -> None:
    def script(v: _V1, rid: int) -> None:
        v._local_channel.emit(_msg(RPC, rid, ["ok"]))
        v._local_channel.emit(_msg(MAP, rid + 1, b"NOT-OURS"))

    v = _V1(script)
    r = await localprobe.local_request(v, "get_map_v1", timeout_s=0.2)
    assert r["map"] is None and r["ack"] == ["ok"]
    src = localprobe.LocalMapSource(v, converter=None, executor=None)
    with pytest.raises(RuntimeError, match="no map frame"):
        await src.refresh()


@pytest.mark.asyncio
async def test_a_real_rpc_result_ends_the_wait_early() -> None:
    def script(v: _V1, rid: int) -> None:
        v._local_channel.emit(_msg(RPC, rid, {"diff": 1}))

    r = await localprobe.local_request(_V1(script), "get_dynamic_map_diff", timeout_s=5)
    assert r["ack"] == {"diff": 1}
    assert r["latency_ms"] < 1000


@pytest.mark.asyncio
async def test_parsed_map_exposed_like_the_library_trait() -> None:
    def script(v: _V1, rid: int) -> None:
        v._local_channel.emit(_msg(MAP, rid, b"MAPBYTES"))

    class _Conv:
        async def async_parse_map_content(self, data: bytes) -> Any:
            return SimpleNamespace(raw_api_response=data, map_data="parsed")

    src = localprobe.LocalMapSource(_V1(script), _Conv(), executor=None)
    await src.refresh()
    assert src.raw_api_response == b"MAPBYTES" and src.map_data == "parsed"
    assert src.via == "local"


@pytest.mark.asyncio
async def test_not_locally_connected() -> None:
    with pytest.raises(RuntimeError, match="not connected locally"):
        await localprobe.local_request(_V1(lambda v, r: None, local_ok=False), "get_map_v1")
