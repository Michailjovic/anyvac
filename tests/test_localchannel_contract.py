"""D2 (docs/49): `localchannel` reaches into python-roborock internals. This pins
every private name it uses against the INSTALLED library, so an upgrade that moves
them fails here — not silently in the field as a dead live poller."""

from __future__ import annotations

import inspect

import pytest

roborock = pytest.importorskip("roborock")


def test_v1_channel_keeps_the_attributes_localchannel_reads() -> None:
    from roborock.devices.rpc.v1_channel import V1Channel

    src = inspect.getsource(V1Channel)
    for attr in ("self._local_channel", "self._mqtt_channel", "self._security_data"):
        assert attr in src, f"V1Channel no longer sets {attr}"
    assert isinstance(inspect.getattr_static(V1Channel, "is_local_connected"), property)


def test_local_channel_keeps_what_local_request_calls() -> None:
    from roborock.devices.transport.local_channel import LocalChannel

    for name in ("subscribe", "publish"):
        assert callable(getattr(LocalChannel, name, None)), f"LocalChannel.{name} is gone"
    assert "protocol_version" in inspect.getsource(LocalChannel)


def test_device_and_integration_coordinator_expose_the_channel() -> None:
    from roborock.devices.device import RoborockDevice

    assert "self._channel" in inspect.getsource(RoborockDevice)
    coordinator = pytest.importorskip("homeassistant.components.roborock.coordinator")
    assert "self._device" in inspect.getsource(coordinator)


def test_request_encoding_names_still_import() -> None:
    from roborock.protocols.v1_protocol import (  # noqa: F401
        RequestMessage,
        create_map_response_decoder,
        decode_rpc_response,
    )
    from roborock.roborock_message import RoborockMessageProtocol

    assert hasattr(RoborockMessageProtocol, "GENERAL_REQUEST")
    assert hasattr(RoborockMessageProtocol, "MAP_RESPONSE")
