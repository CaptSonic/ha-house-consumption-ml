"""
Auto-discovery of HA entities for House Consumption ML.

No configuration needed — the integration scans the running HA instance
and classifies entities by device_class, name patterns, and domain.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword sets for sensor classification
# ---------------------------------------------------------------------------

# Sensors that look like "total house consumption"
_HOUSE_TOTAL_KW = frozenset([
    "house_power", "house_load", "home_power", "home_load",
    "haus_leistung", "hausleistung", "hausverbrauch", "verbrauch_gesamt",
    "gesamt_leistung", "gesamtleistung", "total_power", "total_load",
    "total_consumption", "load_power", "home_consumption",
    "total_active_power",   # e.g. shellypro3em_total_active_power
    "power_consumption",    # common template name for BKW-aware total
])

# Exclude these from house sensors (they measure production/storage/grid)
_EXCLUDE_KW = frozenset([
    "solar", "pv", "photovoltaic", "solarertrag", "ertrag",
    "battery", "akku", "batterie", "speicher", "batt",
    "grid", "netz", "netzbezug", "netzeinspeisung",
    "feed", "einspeis", "export", "import",
    "wind", "turbine",
    "bkw",      # Balkonkraftwerk = balcony solar plant
    "ab2000",  # Zendure/Anker AB2000 BKW battery expansion
    "inverter", "wechselrichter",  # solar/battery inverters (generic)
])

# Entity object_id prefixes that indicate derived/integration sensors (not real appliances)
_APPLIANCE_EXCLUDE_PREFIXES = ("sfml_", "stats_")

# Phase sub-component pattern: _phase_a_, _phase_b_, _phase_c_ (3EM meters etc.)
_PHASE_RE = re.compile(r"_phase_[abc](?:_|$)", re.IGNORECASE)
# Same pattern, for extracting the device prefix
_PHASE_PREFIX_RE = re.compile(r"^(.+?)_phase_[abc](?:_|$)", re.IGNORECASE)

# Patterns that reliably identify the workday sensor
_WORKDAY_PAT = re.compile(r"workday|werktag|arbeitstag", re.IGNORECASE)

# Weather entity priority (first match wins)
_WEATHER_PRIORITY = [
    "fusion", "forecast_home", "forecast", "openweathermap",
    "bright_sky", "home", "local",
]


# ---------------------------------------------------------------------------
# Discovery result container
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    house_power_sensor: str | None          # total house power (W)
    appliance_sensors: list[str]            # individual consumer devices
    person_entities: list[str]              # person.* entities
    weather_entity: str | None              # best weather entity
    workday_sensor: str | None              # binary workday sensor
    calendar_entities: list[str] = field(default_factory=list)  # all available calendars

    def log_summary(self) -> None:
        _LOGGER.info(
            "HCML discovery:\n"
            "  house_power : %s\n"
            "  appliances  : %d sensors\n"
            "  persons     : %s\n"
            "  weather     : %s\n"
            "  workday     : %s\n"
            "  calendars   : %s",
            self.house_power_sensor or "NOT FOUND (will sum appliances)",
            len(self.appliance_sensors),
            self.person_entities or "none",
            self.weather_entity or "none",
            self.workday_sensor or "none (weekday fallback)",
            self.calendar_entities or "none found",
        )
        if self.appliance_sensors:
            _LOGGER.debug("  appliance list: %s", self.appliance_sensors)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_all(
    hass: HomeAssistant,
    user_exclude: frozenset[str] = frozenset(),
) -> DiscoveryResult:
    """Scan the running HA instance and return classified entity lists."""
    house_power = _find_house_power(hass, user_exclude)
    appliances  = _find_appliances(hass, exclude=house_power, user_exclude=user_exclude)
    persons     = _find_persons(hass)
    weather     = _find_weather(hass)
    workday     = _find_workday(hass)
    calendars   = _find_calendars(hass)

    result = DiscoveryResult(
        house_power_sensor=house_power,
        appliance_sensors=appliances,
        person_entities=persons,
        weather_entity=weather,
        workday_sensor=workday,
        calendar_entities=calendars,
    )
    result.log_summary()
    return result


# ---------------------------------------------------------------------------
# Internal finders
# ---------------------------------------------------------------------------

def _name(state) -> str:
    """Return a lower-case combined id+friendly_name for keyword matching."""
    return (
        state.entity_id + " " + state.attributes.get("friendly_name", "")
    ).lower()


def _has_any(text: str, keywords: frozenset) -> bool:
    return any(kw in text for kw in keywords)


def _is_power_sensor(state) -> bool:
    """
    Return True if this state is a power sensor (watts).

    Accepts both the modern way (device_class="power") and the older Shelly /
    custom-integration way where only unit_of_measurement is set.
    """
    attrs = state.attributes

    if attrs.get("device_class") == "power":
        return True

    # Fallback: unit W + name contains a power-related term
    uom = (attrs.get("unit_of_measurement") or "").strip().lower()
    if uom in ("w", "watt", "watts"):
        nm = _name(state)
        return _has_any(nm, frozenset(["leistung", "power", "watt", "load"]))

    return False


def _user_excluded(nm: str, user_exclude: frozenset[str]) -> bool:
    """Return True if any user-configured exclusion fragment appears in nm."""
    return any(kw in nm for kw in user_exclude)


def _find_house_power(hass: HomeAssistant, user_exclude: frozenset[str] = frozenset()) -> str | None:
    """
    Find a single sensor that represents total house power consumption.
    Returns the entity_id or None (caller will fall back to summing appliances).

    Priority (higher = better):
      +3  total sensor of a multi-phase meter (3EM at mains) — most reliable
      +2  contains "house" or "haus"
      +1  contains "gesamt" or "total"
      +1  suffix "_active_power" (tie-break: prefer active over generic total)
    """
    # Pre-scan: find device prefixes that have phase sub-sensors.
    # A device with phase_a/b/c sensors is a 3-phase meter at the mains —
    # its total sensor is almost certainly the whole-house power reading.
    multi_phase_prefixes: set[str] = set()
    for state in hass.states.async_all("sensor"):
        m = _PHASE_PREFIX_RE.match(state.entity_id.split(".")[-1])
        if m:
            multi_phase_prefixes.add(m.group(1))

    candidates: list[tuple[int, str]] = []  # (priority, entity_id)

    for state in hass.states.async_all("sensor"):
        if not _is_power_sensor(state):
            continue

        nm     = _name(state)
        eid_obj = state.entity_id.split(".")[-1]

        if _has_any(nm, _EXCLUDE_KW):
            continue
        if _user_excluded(nm, user_exclude):
            continue
        if not _has_any(nm, _HOUSE_TOTAL_KW):
            continue

        prio = 0
        # Explicitly named consumption sensor (BKW-aware template) → top priority
        if "power_consumption" in eid_obj:
            prio += 6
        # Physical 3-phase total at mains → second best
        elif any(eid_obj.startswith(f"{pfx}_total") for pfx in multi_phase_prefixes):
            prio += 3
        elif "house" in nm or "haus" in nm:
            prio += 2

        if "gesamt" in nm or "total" in nm:
            prio += 1
        if eid_obj.endswith("_active_power"):
            prio += 1  # prefer active-power over generic total

        candidates.append((prio, state.entity_id))
        _LOGGER.debug("House power candidate: %s (prio=%d)", state.entity_id, prio)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _find_appliances(
    hass: HomeAssistant,
    exclude: str | None,
    user_exclude: frozenset[str] = frozenset(),
) -> list[str]:
    """
    Find individual consumer/appliance power sensors.

    Exclusions (in order):
    - Solar, battery, grid, BKW (keyword filter)
    - The dedicated house-total sensor passed via `exclude`
    - Any sensor matching house-total keywords (aggregates)
    - Sensors from derived/integration prefixes (sfml_, stats_)
    - Shelly "device_power" diagnostics (~1 W self-consumption)
    - Phase sub-sensors of multi-phase meters (phase_a/b/c + their totals)
    - Unavailable/non-numeric sensors
    """
    # First pass — collect candidates and detect multi-phase device prefixes
    raw: list[tuple[str, str]] = []      # (entity_id, eid_obj)
    multi_phase_prefixes: set[str] = set()

    for state in hass.states.async_all("sensor"):
        if not _is_power_sensor(state):
            continue
        if state.entity_id == exclude:
            continue

        nm     = _name(state)
        eid_obj = state.entity_id.split(".")[-1]

        if _has_any(nm, _EXCLUDE_KW):
            continue
        if _user_excluded(nm, user_exclude):
            continue
        if _has_any(nm, _HOUSE_TOTAL_KW):
            continue  # aggregate/total sensor, not an individual appliance
        if any(eid_obj.startswith(p) for p in _APPLIANCE_EXCLUDE_PREFIXES):
            continue  # derived integration sensor (sfml_, stats_, …)
        if "device_power" in state.entity_id:
            continue  # Shelly self-consumption diagnostic

        try:
            float(state.state)
        except (ValueError, TypeError):
            continue  # unavailable / non-numeric

        # Track multi-phase meter prefixes so we can remove their totals below
        m = _PHASE_PREFIX_RE.match(eid_obj)
        if m:
            multi_phase_prefixes.add(m.group(1))

        raw.append((state.entity_id, eid_obj))

    # Second pass — filter out phase sub-sensors and their aggregate totals
    result: list[str] = []
    for eid, eid_obj in raw:
        if _PHASE_RE.search(eid_obj):
            continue  # phase sub-sensor (already tracked its prefix above)
        if any(eid_obj.startswith(f"{pfx}_total") for pfx in multi_phase_prefixes):
            continue  # aggregate total from a multi-phase meter
        result.append(eid)
        _LOGGER.debug("Appliance discovered: %s", eid)

    result.sort()
    return result


def _find_persons(hass: HomeAssistant) -> list[str]:
    return sorted(s.entity_id for s in hass.states.async_all("person"))


def _find_weather(hass: HomeAssistant) -> str | None:
    states = list(hass.states.async_all("weather"))
    if not states:
        return None

    for priority_kw in _WEATHER_PRIORITY:
        for s in states:
            if priority_kw in s.entity_id.lower():
                return s.entity_id

    return states[0].entity_id


def _find_workday(hass: HomeAssistant) -> str | None:
    for state in hass.states.async_all("binary_sensor"):
        if _WORKDAY_PAT.search(state.entity_id):
            return state.entity_id
    return None


def _find_calendars(hass: HomeAssistant) -> list[str]:
    """Return all calendar entity IDs sorted alphabetically.
    Shown in the discovery diagnostic sensor so the user knows which IDs
    to put into the 'calendars:' config option.
    """
    return sorted(s.entity_id for s in hass.states.async_all("calendar"))
