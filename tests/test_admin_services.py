"""1.51.9 (plan po auditu C3): file and diagnostic services are admin-only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import Unauthorized, UnknownUser

import custom_components.anyvac.services as services_mod
from custom_components.anyvac.services import ADMIN_SERVICES, _require_admin

pytestmark = pytest.mark.asyncio

USERS = {
    "admin": SimpleNamespace(is_admin=True),
    "user": SimpleNamespace(is_admin=False),
}


def _hass():
    async def _get_user(uid):
        return USERS.get(uid)

    return SimpleNamespace(auth=SimpleNamespace(async_get_user=_get_user))


async def _handler(call):
    return {"ok": True}


def _call(user_id):
    return SimpleNamespace(context=SimpleNamespace(user_id=user_id), data={})


async def test_admin_may_call() -> None:
    assert await _require_admin(_hass(), _handler)(_call("admin")) == {"ok": True}


async def test_system_call_without_user_is_allowed() -> None:
    assert await _require_admin(_hass(), _handler)(_call(None)) == {"ok": True}


async def test_non_admin_is_refused() -> None:
    with pytest.raises(Unauthorized):
        await _require_admin(_hass(), _handler)(_call("user"))


async def test_unknown_user_is_refused() -> None:
    with pytest.raises(UnknownUser):
        await _require_admin(_hass(), _handler)(_call("ghost"))


def test_admin_set_is_exactly_the_file_and_debug_services() -> None:
    assert ADMIN_SERVICES == {
        "snapshot_map_as_floorplan",
        "export_map_guide",
        "dump_raw_map",
        "probe_map_rate",
        "detect_floorplan_fiducials",
    }
    # Everything the card uses in normal operation stays open to every user.
    for name in ("clean", "plan", "goto", "zone_clean", "cancel", "select_rooms", "set_layers"):
        assert name not in ADMIN_SERVICES
    assert services_mod.DEBUG_DUMP_DIR == "anyvac/debug"
