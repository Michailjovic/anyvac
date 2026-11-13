"""1.51.8 (plan po auditu B2): unload stops orchestration and flushes pending
Store writes; removing the last entry deletes the `.storage` files."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.anyvac as integration
import custom_components.anyvac.coordinator as coordinator_mod
from custom_components.anyvac.const import DOMAIN, STORE_KEYS

pytestmark = pytest.mark.asyncio

SRC = Path(coordinator_mod.__file__).read_text(encoding="utf-8")


def test_store_keys_match_the_stores_the_coordinator_creates() -> None:
    created = set(re.findall(r'Store\(hass, 1, f"\{DOMAIN\}_([a-z_]+)"\)', SRC))
    assert created == set(STORE_KEYS)


def test_no_store_bypasses_the_flush_registry() -> None:
    assert ".async_delay_save(" not in SRC.replace("store.async_delay_save(data_func, delay)", "")


class _FakeStore:
    def __init__(self, key: str) -> None:
        self.key = key
        self.saved: list[Any] = []
        self.delayed: list[Any] = []

    def async_delay_save(self, func, delay) -> None:
        self.delayed.append(func)

    async def async_save(self, data) -> None:
        self.saved.append(data)


async def test_shutdown_writes_every_pending_store_once() -> None:
    coord = object.__new__(coordinator_mod.AnyVacCoordinator)
    coord._pending_saves = {}
    a, b = _FakeStore("anyvac_a"), _FakeStore("anyvac_b")
    state = {"n": 1}
    coord._delay_save(a, lambda: dict(state), 5)
    state["n"] = 2
    coord._delay_save(a, lambda: dict(state), 5)
    coord._delay_save(b, lambda: ["x"], 2)
    await coord._flush_stores()
    assert a.saved == [{"n": 2}] and b.saved == [["x"]]
    await coord._flush_stores()
    assert a.saved == [{"n": 2}]


async def test_one_failing_store_does_not_block_the_rest() -> None:
    coord = object.__new__(coordinator_mod.AnyVacCoordinator)
    coord._pending_saves = {}
    bad, good = _FakeStore("anyvac_bad"), _FakeStore("anyvac_good")

    async def _boom(_data):
        raise OSError("disk full")

    bad.async_save = _boom
    coord._delay_save(bad, lambda: {}, 1)
    coord._delay_save(good, lambda: {"ok": True}, 1)
    await coord._flush_stores()
    assert good.saved == [{"ok": True}]


async def test_remove_last_entry_deletes_every_store(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[str] = []

    class _Store:
        def __init__(self, _hass, _ver, key) -> None:
            self.key = key

        async def async_remove(self) -> None:
            removed.append(self.key)

    monkeypatch.setattr(integration, "Store", _Store)
    entry = SimpleNamespace(entry_id="e1")
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda d: [entry]))
    await integration.async_remove_entry(hass, entry)
    assert removed == [f"{DOMAIN}_{k}" for k in STORE_KEYS]


async def test_remove_keeps_stores_while_another_entry_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[str] = []
    monkeypatch.setattr(integration, "Store", lambda *a: pytest.fail("must not touch storage"))
    entry, other = SimpleNamespace(entry_id="e1"), SimpleNamespace(entry_id="e2")
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda d: [entry, other]))
    await integration.async_remove_entry(hass, entry)
    assert removed == []


async def test_unload_cancels_running_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    finished: list[str] = []
    job = SimpleNamespace(started_vacuums={"vacuum.s8"}, finish=lambda: finished.append("job"))

    async def _unload_platforms(_entry, _platforms):
        return True

    hass = SimpleNamespace(
        data={DOMAIN: {"jobs": [job]}},
        config_entries=SimpleNamespace(async_unload_platforms=_unload_platforms),
        services=SimpleNamespace(has_service=lambda d, s: False),
    )
    assert await integration.async_unload_entry(hass, SimpleNamespace()) is True
    assert finished == ["job"]
