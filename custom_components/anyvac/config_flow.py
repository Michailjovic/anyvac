"""Config flow for AnyVac.

AnyVac needs no credentials of its own — it reads data from the already
configured Roborock integration. The setup flow is therefore a single
confirmation step and only one instance is allowed.

The options flow carries a single opt-in: whether to keep publishing the legacy
mm-space path attributes (see ``OPT_EXPOSE_LEGACY_MM``).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import DEFAULT_EXPOSE_LEGACY_MM, DOMAIN, OPT_EXPOSE_LEGACY_MM


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return AnyVacOptionsFlow()


class AnyVacOptionsFlow(OptionsFlow):
    """Options for AnyVac."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        OPT_EXPOSE_LEGACY_MM,
                        default=self.config_entry.options.get(
                            OPT_EXPOSE_LEGACY_MM, DEFAULT_EXPOSE_LEGACY_MM
                        ),
                    ): bool,
                }
            ),
        )
