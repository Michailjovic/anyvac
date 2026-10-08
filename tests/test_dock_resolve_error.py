"""`anyvac.dock_resolve_error` (docs/47 §3).

The service handler is a closure inside `async_register_services`; what is
tested is the logic factored out of it (`_resolve_dock_error`) and the
`dock_status.dock_error` name the card shows (`_dock_error_name`).
"""

from __future__ import annotations

from enum import IntEnum
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.anyvac.coordinator import _dock_error_name
from custom_components.anyvac.services import _resolve_dock_error


class _DockErr(IntEnum):  # shape of python-roborock's RoborockDockErrorCode
    ok = 0
    water_empty = 38


class _Raw:
    def __init__(self) -> None:
        self.codes: list[int] = []

    async def __call__(self, code: int) -> None:
        self.codes.append(code)


class _StatusWithResolve:
    """python-roborock >= 7.12 StatusTrait."""

    def __init__(self, err: Any) -> None:
        self.dock_error_status = err
        self.resolved: list[Any] = []

    async def resolve_error(self, error_code: int | None = None) -> None:
        self.resolved.append(error_code)


@pytest.mark.asyncio
async def test_no_status_or_no_error_does_nothing() -> None:
    raw = _Raw()
    assert await _resolve_dock_error(None, raw) is False
    assert await _resolve_dock_error(SimpleNamespace(dock_error_status=_DockErr.ok), raw) is False
    assert await _resolve_dock_error(SimpleNamespace(dock_error_status=None), raw) is False
    assert raw.codes == []


@pytest.mark.asyncio
async def test_library_method_gets_the_dock_code_explicitly() -> None:
    """Without a code the library would fall through to the ROBOT error."""
    raw = _Raw()
    status = _StatusWithResolve(_DockErr.water_empty)
    assert await _resolve_dock_error(status, raw) is True
    assert status.resolved == [38]
    assert raw.codes == []


@pytest.mark.asyncio
async def test_older_library_sends_the_raw_command() -> None:
    raw = _Raw()
    status = SimpleNamespace(dock_error_status=_DockErr.water_empty)
    assert await _resolve_dock_error(status, raw) is True
    assert raw.codes == [38]


def test_dock_error_name() -> None:
    assert _dock_error_name(None) is None
    assert _dock_error_name(_DockErr.ok) is None
    assert _dock_error_name(_DockErr.water_empty) == "water_empty"
    assert _dock_error_name(41) == "code_41"
    assert _dock_error_name("garbage") is None
