"""Config flow for AnyVac.

AnyVac needs no credentials of its own — it reads data from the already
configured Roborock integration. The flow is therefore a single confirmation
step and only one instance is allowed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class AnyVacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AnyVac."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="AnyVac", data={})
        return self.async_show_form(step_id="user")
