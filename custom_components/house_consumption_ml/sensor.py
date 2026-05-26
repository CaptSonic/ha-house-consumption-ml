"""Sensor entities for House Consumption ML Forecast."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FORECAST_DAYS
from .coordinator import HCMLCoordinator

_LOGGER = logging.getLogger(__name__)

_OFFSET_LABELS = [
    "Heute", "Morgen", "Übermorgen",
    *[f"Tag +{i}" for i in range(3, FORECAST_DAYS)],
]


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict,
    async_add_entities,
    discovery_info=None,
) -> None:
    coordinator: HCMLCoordinator = hass.data[DOMAIN]

    entities: list[SensorEntity] = [
        HCMLWeeklyForecastSensor(coordinator),
        HCMLModelStatusSensor(coordinator),
        HCMLDiscoverySensor(coordinator),
    ]
    for i in range(FORECAST_DAYS):
        entities.append(HCMLDayForecastSensor(coordinator, i))

    async_add_entities(entities, True)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _Base(CoordinatorEntity[HCMLCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HCMLCoordinator, uid_suffix: str | None = None) -> None:
        super().__init__(coordinator)
        # Only set unique_id from argument when given; subclasses that define
        # _attr_unique_id as a class attribute keep their own value.
        if uid_suffix is not None:
            self._attr_unique_id = f"hcml_{uid_suffix}"

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}


# ---------------------------------------------------------------------------
# Per-day sensor (7×)
# ---------------------------------------------------------------------------

class HCMLDayForecastSensor(_Base):
    _attr_native_unit_of_measurement  = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class                 = SensorStateClass.MEASUREMENT
    _attr_icon                        = "mdi:lightning-bolt-circle"

    def __init__(self, coordinator: HCMLCoordinator, day_idx: int) -> None:
        super().__init__(coordinator, f"day_{day_idx}")
        self._day_idx = day_idx
        self._attr_name = f"HCML Prognose {_OFFSET_LABELS[day_idx]}"

    @property
    def _day(self) -> dict | None:
        days = self._data.get("days", [])
        return days[self._day_idx] if self._day_idx < len(days) else None

    @property
    def native_value(self) -> float | None:
        d = self._day
        return d["predicted_kwh"] if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._day
        if not d:
            return {}
        hourly        = d.get("hourly_kwh", [])
        hourly_base   = d.get("hourly_base_kwh", [])
        hourly_device = d.get("hourly_device_kwh", [])
        return {
            "date":              d["date"],
            "day_name":          d["day_name"],
            "base_kwh":          d.get("base_kwh"),
            "device_kwh":        d.get("device_kwh"),
            "hourly_kwh":        hourly,
            "hourly_base_kwh":   hourly_base,
            "hourly_device_kwh": hourly_device,
            "hourly_detail": {
                f"{h:02d}:00": {
                    "total":  hourly[h]        if h < len(hourly)        else 0.0,
                    "base":   hourly_base[h]   if h < len(hourly_base)   else 0.0,
                    "device": hourly_device[h] if h < len(hourly_device) else 0.0,
                }
                for h in range(24)
            },
            "peak_hour": _peak_hour(hourly),
            "night_kwh": round(sum(hourly[:6] + hourly[22:]), 3),
            "day_kwh":   round(sum(hourly[6:22]), 3),
        }


# ---------------------------------------------------------------------------
# 7-day overview
# ---------------------------------------------------------------------------

class HCMLWeeklyForecastSensor(_Base):
    _attr_name                        = "HCML 7-Tage Prognose"
    _attr_unique_id                   = "hcml_weekly"
    _attr_native_unit_of_measurement  = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class                 = SensorStateClass.MEASUREMENT
    _attr_icon                        = "mdi:chart-timeline-variant"

    @property
    def native_value(self) -> float | None:
        return self._data.get("total_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "days":       self._data.get("days", []),
            "updated_at": self._data.get("updated_at"),
        }


# ---------------------------------------------------------------------------
# Model status (diagnostic)
# ---------------------------------------------------------------------------

class HCMLModelStatusSensor(_Base):
    _attr_name            = "HCML Modell Status"
    _attr_unique_id       = "hcml_model_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon            = "mdi:brain"
    _attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> float | None:
        m = self._data.get("model", {})
        return m.get("r2_pct") if m.get("fitted") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._data.get("model", {})
        return {
            **m,
            "updated_at": self._data.get("updated_at"),
        }


# ---------------------------------------------------------------------------
# Discovery info (diagnostic)
# ---------------------------------------------------------------------------

class HCMLDiscoverySensor(_Base):
    """Shows which entities were auto-discovered — useful for debugging."""
    _attr_name            = "HCML Discovery"
    _attr_unique_id       = "hcml_discovery"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon            = "mdi:magnify-scan"

    @property
    def native_value(self) -> str:
        disc = self._data.get("discovery", {})
        n_dev  = disc.get("n_appliances", 0)
        n_pers = len(disc.get("persons", []))
        return f"{n_pers} Personen, {n_dev} Geräte"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._data.get("discovery", {})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _peak_hour(hourly: list[float]) -> str | None:
    if not hourly:
        return None
    return f"{max(range(len(hourly)), key=lambda i: hourly[i]):02d}:00"
