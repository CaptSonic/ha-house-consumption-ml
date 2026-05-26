"""House Consumption ML Forecast — zero-config HA integration."""
from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.discovery import async_load_platform

from .const import (
    CONF_DB_PATH, CONF_SFML_DB_PATH, CONF_HOUSE_POWER_SENSOR,
    CONF_EXCLUDE_DEVICES, DEFAULT_DB_PATH, DEFAULT_SFML_DB_PATH, DOMAIN,
)
from .coordinator import HCMLCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DB_PATH,            default=DEFAULT_DB_PATH):      cv.string,
                vol.Optional(CONF_SFML_DB_PATH,       default=DEFAULT_SFML_DB_PATH): cv.string,
                vol.Optional(CONF_HOUSE_POWER_SENSOR, default=""):                   cv.string,
                vol.Optional(CONF_EXCLUDE_DEVICES,    default=[]):
                    vol.All(cv.ensure_list, [cv.string]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    conf = config.get(DOMAIN, {})
    db_path            = conf.get(CONF_DB_PATH,            DEFAULT_DB_PATH)
    sfml_db_path       = conf.get(CONF_SFML_DB_PATH,       DEFAULT_SFML_DB_PATH)
    house_power_sensor = conf.get(CONF_HOUSE_POWER_SENSOR, "")
    exclude_devices    = conf.get(CONF_EXCLUDE_DEVICES,    [])

    coordinator = HCMLCoordinator(
        hass,
        db_path=db_path,
        sfml_db_path=sfml_db_path,
        house_power_sensor=house_power_sensor,
        exclude_devices=exclude_devices,
    )
    hass.data[DOMAIN] = coordinator

    await coordinator.async_setup()

    hass.async_create_task(
        async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )
    return True
