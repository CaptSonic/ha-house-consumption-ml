"""House Consumption ML Forecast — Home Assistant integration."""
from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
from .coordinator import HCMLCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

# ---------------------------------------------------------------------------
# YAML schema — kept for backward-compat / import migration
# ---------------------------------------------------------------------------

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DB_PATH,            default=DEFAULT_DB_PATH):      cv.string,
                vol.Optional(CONF_SFML_DB_PATH,       default=DEFAULT_SFML_DB_PATH): cv.string,
                vol.Optional(CONF_HOUSE_POWER_SENSOR, default=""):                   cv.string,
                vol.Optional(CONF_EXCLUDE_DEVICES,    default=[]):
                    vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_CALENDARS,          default=[]):
                    vol.All(cv.ensure_list, [cv.string]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """
    Called when HA processes configuration.yaml.

    If the integration is configured via YAML and no config entry exists yet,
    trigger an import flow so the settings are migrated to a config entry.
    After migration the user should remove the YAML block.
    """
    if DOMAIN not in config:
        return True
    if hass.config_entries.async_entries(DOMAIN):
        # Already set up via config entry — YAML is now redundant
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=config[DOMAIN],
        )
    )
    return True


# ---------------------------------------------------------------------------
# Config-entry lifecycle
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up House Consumption ML from a config entry."""
    options = entry.options

    # exclude_devices is stored as a list; guard against legacy str format
    exclude_raw = options.get(CONF_EXCLUDE_DEVICES, [])
    if isinstance(exclude_raw, str):
        exclude_list = [s.strip() for s in exclude_raw.splitlines() if s.strip()]
    else:
        exclude_list = list(exclude_raw)

    coordinator = HCMLCoordinator(
        hass,
        db_path=options.get(CONF_DB_PATH, DEFAULT_DB_PATH),
        sfml_db_path=options.get(CONF_SFML_DB_PATH, DEFAULT_SFML_DB_PATH),
        house_power_sensor=options.get(CONF_HOUSE_POWER_SENSOR) or "",
        exclude_devices=exclude_list,
        calendars=list(options.get(CONF_CALENDARS) or []),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry whenever the user saves new options
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
