"""Config flow for House Consumption ML Forecast."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CALENDARS,
    CONF_DB_PATH,
    CONF_EXCLUDE_DEVICES,
    CONF_HOUSE_POWER_SENSOR,
    CONF_SFML_DB_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_SFML_DB_PATH,
    DOMAIN,
)


class HCMLConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for House Consumption ML Forecast."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial setup step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            options = _process_user_input(user_input)
            return self.async_create_entry(
                title="House Consumption ML",
                data={},
                options=options,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema({}),
        )

    async def async_step_import(
        self, import_data: dict
    ) -> config_entries.FlowResult:
        """Import configuration from configuration.yaml (called on first start)."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        options = {
            CONF_HOUSE_POWER_SENSOR: import_data.get(CONF_HOUSE_POWER_SENSOR, ""),
            CONF_CALENDARS:          list(import_data.get(CONF_CALENDARS, [])),
            CONF_EXCLUDE_DEVICES:    list(import_data.get(CONF_EXCLUDE_DEVICES, [])),
            CONF_DB_PATH:            import_data.get(CONF_DB_PATH, DEFAULT_DB_PATH),
            CONF_SFML_DB_PATH:       import_data.get(CONF_SFML_DB_PATH, DEFAULT_SFML_DB_PATH),
        }
        return self.async_create_entry(
            title="House Consumption ML",
            data={},
            options=options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> HCMLOptionsFlow:
        return HCMLOptionsFlow(config_entry)


class HCMLOptionsFlow(config_entries.OptionsFlow):
    """Handle options (re-configuration) for HCML."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Show options form."""
        if user_input is not None:
            return self.async_create_entry(data=_process_user_input(user_input))

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(self._config_entry.options),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_schema(current: dict) -> vol.Schema:
    """Build the options / setup form schema with current values pre-filled."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_HOUSE_POWER_SENSOR,
                description={
                    "suggested_value": current.get(CONF_HOUSE_POWER_SENSOR) or ""
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=False)
            ),
            vol.Optional(
                CONF_CALENDARS,
                description={
                    "suggested_value": current.get(CONF_CALENDARS) or []
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar", multiple=True)
            ),
            vol.Optional(
                CONF_EXCLUDE_DEVICES,
                description={
                    "suggested_value": "\n".join(
                        current.get(CONF_EXCLUDE_DEVICES) or []
                    )
                },
            ): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional(
                CONF_DB_PATH,
                description={
                    "suggested_value": current.get(CONF_DB_PATH, DEFAULT_DB_PATH)
                },
            ): selector.TextSelector(),
            vol.Optional(
                CONF_SFML_DB_PATH,
                description={
                    "suggested_value": current.get(CONF_SFML_DB_PATH, DEFAULT_SFML_DB_PATH)
                },
            ): selector.TextSelector(),
        }
    )


def _process_user_input(user_input: dict) -> dict:
    """Convert raw form data to storable options dict."""
    processed = dict(user_input)
    # Multiline text → list of non-empty stripped lines
    raw_exclude = processed.get(CONF_EXCLUDE_DEVICES, "") or ""
    processed[CONF_EXCLUDE_DEVICES] = [
        s.strip() for s in raw_exclude.splitlines() if s.strip()
    ]
    # Ensure calendars is always a list
    processed[CONF_CALENDARS] = list(processed.get(CONF_CALENDARS) or [])
    # Normalise empty string to "" (not None)
    processed[CONF_HOUSE_POWER_SENSOR] = processed.get(CONF_HOUSE_POWER_SENSOR) or ""
    return processed
