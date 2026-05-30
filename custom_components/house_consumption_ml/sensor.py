"""Sensor entities for House Consumption ML Forecast."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FORECAST_DAYS
from .coordinator import HCMLCoordinator

_LOGGER = logging.getLogger(__name__)

_OFFSET_LABELS = [
    "Heute", "Morgen", "Übermorgen",
    *[f"Tag +{i}" for i in range(3, FORECAST_DAYS)],
]

_DE_WEEKDAYS = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HCMLCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        HCMLWeeklyForecastSensor(coordinator),
        HCMLModelStatusSensor(coordinator),
        HCMLDiscoverySensor(coordinator),
        HCMLSnapshotSensor(coordinator),
        HCMLForecastAccuracySensor(coordinator),
    ]
    for i in range(FORECAST_DAYS):
        entities.append(HCMLDayForecastSensor(coordinator, i))

    async_add_entities(entities, True)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _Base(CoordinatorEntity[HCMLCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "hcml_forecast")},
        name="House Consumption ML",
        manufacturer="CaptSonic",
        model="Ridge Regression Forecast",
        entry_type=DeviceEntryType.SERVICE,
    )

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
        # Days 0–2 keep static names; days 3–6 resolve the name dynamically
        # via the `name` property so the actual weekday is shown.
        if day_idx < 3:
            self._attr_name = f"Prognose {_OFFSET_LABELS[day_idx]}"

    @property
    def name(self) -> str:
        if self._day_idx < 3:
            return self._attr_name  # type: ignore[return-value]
        d = self._day
        if d:
            try:
                wday = datetime.strptime(d["date"], "%Y-%m-%d").weekday()
                return f"Prognose {_DE_WEEKDAYS[wday]}"
            except Exception:
                pass
        return f"Prognose Tag +{self._day_idx}"

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
        hourly_actual = d.get("hourly_actual_kwh") or []  # actual measured kWh per hour (today only)
        try:
            day_name_de = _DE_WEEKDAYS[datetime.strptime(d["date"], "%Y-%m-%d").weekday()]
        except Exception:
            day_name_de = None
        return {
            "date":              d["date"],
            "day_name":          d["day_name"],
            "day_name_de":       day_name_de,
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
            "hourly_actual_kwh": hourly_actual,
            "peak_hour": _peak_hour(hourly),
            "night_kwh": round(sum(hourly[:6] + hourly[22:]), 3),
            "day_kwh":   round(sum(hourly[6:22]), 3),
        }


# ---------------------------------------------------------------------------
# 7-day overview
# ---------------------------------------------------------------------------

class HCMLWeeklyForecastSensor(_Base):
    _attr_name                        = "7-Tage Prognose"
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
            "nightly_at": self._data.get("nightly_at"),
            "updated_at": self._data.get("updated_at"),
        }


# ---------------------------------------------------------------------------
# Model status (diagnostic)
# ---------------------------------------------------------------------------

class HCMLModelStatusSensor(_Base):
    _attr_name            = "Modell Status"
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
            "nightly_at": self._data.get("nightly_at"),
            "updated_at": self._data.get("updated_at"),
        }


# ---------------------------------------------------------------------------
# Discovery info (diagnostic)
# ---------------------------------------------------------------------------

class HCMLDiscoverySensor(_Base):
    """Shows which entities were auto-discovered — useful for debugging."""
    _attr_name            = "Discovery"
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
# Daily actual snapshot (diagnostic)
# ---------------------------------------------------------------------------

class HCMLSnapshotSensor(_Base):
    """Yesterday's actual consumption — written once per day around midnight."""

    _attr_name                       = "Ist-Verbrauch Gestern"
    _attr_unique_id                  = "hcml_ist_gestern"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:check-circle-outline"
    _attr_entity_category            = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        s = self._data.get("snapshot")
        return s["actual_kwh"] if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._data.get("snapshot")
        if not s:
            return {}
        return {
            "date":        s["date"],
            "hours_count": s["hours_count"],
            "hourly_wh":   s["hourly_wh"],
            "recorded_at": s["recorded_at"],
        }


# ---------------------------------------------------------------------------
# Forecast accuracy (diagnostic)
# ---------------------------------------------------------------------------

class HCMLForecastAccuracySensor(_Base):
    """
    Shows how accurately yesterday morning's frozen forecast matched the
    actual consumption, plus a device-level explanation of the delta.
    """

    _attr_name                       = "Prognose-Genauigkeit"
    _attr_unique_id                  = "hcml_prognose_genauigkeit"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:chart-line-variant"
    _attr_entity_category            = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        a = self._data.get("accuracy")
        return a["accuracy_pct"] if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self._data.get("accuracy")
        if not a:
            return {}
        hourly_wh    = a.get("hourly_wh") or []
        forecast_kwh = round(sum(v or 0 for v in hourly_wh) / 1000.0, 2)
        return {
            "date":         a["date"],
            "forecast_kwh": forecast_kwh,
            "actual_kwh":   round(forecast_kwh + (a["delta_kwh"] or 0), 2),
            "delta_kwh":    a["delta_kwh"],
            "explanation":  a.get("explanation") or [],
            "frozen_at":    a["frozen_at"],
            "hourly_wh":    hourly_wh,   # frozen forecast Wh per hour (for comparison charts)
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _peak_hour(hourly: list[float]) -> str | None:
    if not hourly:
        return None
    return f"{max(range(len(hourly)), key=lambda i: hourly[i]):02d}:00"
