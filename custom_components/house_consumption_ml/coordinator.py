"""
DataUpdateCoordinator — auto-discovers entities, collects data,
trains model, and produces a 7-day hourly consumption forecast.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BOOTSTRAP_DAYS,
    DEVICE_ON_THRESHOLD_W,
    DOMAIN,
    DRIFT_MIN_DAYS,
    DRIFT_WARNING_THRESHOLD_PCT,
    FORECAST_DAYS,
    LARGE_DEVICE_THRESHOLD_W,
    MIN_SAMPLES,
    OUTLIER_STD,
    PLAUSIBILITY_MAX_W,
    PLAUSIBILITY_MIN_W,
    RIDGE_ALPHA,
    SNAPSHOT_MIN_HOURS,
    UPDATE_INTERVAL_MINUTES,
)
from .db import HCMLDatabase
from .discovery import DiscoveryResult, discover_all
from .model import RidgeModel, build_features

_LOGGER = logging.getLogger(__name__)
_WEATHER_CACHE_TTL = timedelta(hours=3)


class HCMLCoordinator(DataUpdateCoordinator[dict[str, Any]]):

    def __init__(
        self,
        hass: HomeAssistant,
        db_path: str,
        sfml_db_path: str = "",
        house_power_sensor: str = "",
        exclude_devices: list[str] | None = None,
        calendars: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._db_path = db_path
        self._sfml_db_path = sfml_db_path
        self._configured_house_power = house_power_sensor
        self._exclude_devices = frozenset(s.lower() for s in (exclude_devices or []))
        self._calendar_ids: list[str] = list(calendars or [])
        self._holiday_dates: set[str] = set()   # YYYY-MM-DD strings, refreshed each update
        self._db: HCMLDatabase | None = None
        self._discovery: DiscoveryResult | None = None
        self._model = RidgeModel(alpha=RIDGE_ALPHA)

        # Derived from discovery
        self._house_power_sensor: str | None = None
        self._appliance_sensors: list[str] = []
        self._person_ids: list[str] = []
        self._weather_entity: str | None = None
        self._workday_sensor: str | None = None

        # Cached look-up tables
        self._hourly_means: dict[tuple[int, int], float] = {}
        self._presence_patterns: dict[tuple[int, int, str], float] = {}
        self._device_patterns: dict[tuple[int, int, str], float] = {}

        # Weather cache
        self._wx_cache: list[dict] | None = None
        self._wx_cache_at: datetime | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """
        Initialise the integration.

        Discovery is deferred until EVENT_HOMEASSISTANT_STARTED so that all
        other integrations (SFML, Shelly, …) have finished setting up their
        entities before we scan for them.  If HA is already running (e.g.
        after a config reload) we skip straight to discovery.
        """
        _LOGGER.info("House Consumption ML: registering startup hook")
        self._db = HCMLDatabase(self._db_path)

        if self.hass.is_running:
            # Config reload / already started
            await self._async_post_start()
        else:
            @callback
            def _on_started(_event) -> None:
                self.hass.async_create_task(self._async_post_start())

            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

    async def _async_post_start(self) -> None:
        """Run after HA is fully started: discover, bootstrap, first update."""
        try:
            _LOGGER.info("House Consumption ML: starting auto-discovery…")
            self._discovery = discover_all(self.hass, self._exclude_devices)
            self._apply_discovery(self._discovery)

            if self._db.count() < MIN_SAMPLES:
                imported = await self.hass.async_add_executor_job(
                    self._bootstrap_from_sfml_db, self._sfml_db_path
                )
                if imported == 0:
                    await self._bootstrap_from_recorder()

            await self.async_refresh()
        except Exception as exc:
            _LOGGER.error("HCML startup failed: %s", exc, exc_info=True)

    def _apply_discovery(self, d: DiscoveryResult) -> None:
        self._person_ids        = d.person_entities
        self._weather_entity    = d.weather_entity
        self._workday_sensor    = d.workday_sensor
        self._appliance_sensors = d.appliance_sensors

        # Explicit config always wins over auto-discovery
        if self._configured_house_power:
            self._house_power_sensor = self._configured_house_power
            _LOGGER.info(
                "House power sensor (configured): %s", self._configured_house_power
            )
        elif d.house_power_sensor:
            self._house_power_sensor = d.house_power_sensor
        elif d.appliance_sensors:
            # No dedicated total sensor → will sum all appliances live
            self._house_power_sensor = None
            _LOGGER.info(
                "No total-house-power sensor found; "
                "computing total from %d appliance sensors",
                len(d.appliance_sensors),
            )
        else:
            _LOGGER.warning(
                "No house power sensor and no appliance sensors found. "
                "Predictions will be poor until data accumulates."
            )

    # ------------------------------------------------------------------
    # DataUpdateCoordinator
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        assert self._db is not None

        # Refresh calendar-based holiday dates for the next 8 days
        self._holiday_dates = await self._get_holiday_dates()

        # Always: collect data point + train model
        await self._collect_datapoint()
        await self._maybe_save_snapshot()
        await self.hass.async_add_executor_job(self._train_model)

        # Between 00:00–05:59: build and persist the nightly forecast once
        if dt_util.now().hour < 6:
            await self._maybe_refresh_nightly_forecast()

        # Load the stored nightly forecast (live fallback on very first start)
        nightly = await self.hass.async_add_executor_job(
            self._db.get_latest_nightly_forecast
        )
        if nightly is not None:
            days       = nightly["days"]
            total_kwh  = nightly["total_kwh"]
            nightly_at = nightly["created_at"]
        else:
            # No nightly forecast stored yet — build live as one-time fallback
            live       = await self._build_forecast()
            days       = live["days"]
            total_kwh  = live["total_kwh"]
            nightly_at = None

        # Enrich today's forecast (days[0]) with actual consumption measured so far
        if days:
            today_str  = dt_util.now().strftime("%Y-%m-%d")
            today_rows = await self.hass.async_add_executor_job(
                self._db.get_rows_for_local_date, today_str
            )
            by_hour = {
                r["hour"]: round(r["consumption_wh"] / 1000.0, 3)
                for r in today_rows
            }
            days[0]["hourly_actual_kwh"] = [by_hour.get(h) for h in range(24)]

        # Accuracy tracking (uses days[0] for forecast_snapshots)
        await self._maybe_freeze_and_evaluate(days)

        drift_detected, avg_acc_7d = await self.hass.async_add_executor_job(
            self._check_drift
        )

        d = self._discovery
        return {
            "days":       days,
            "total_kwh":  total_kwh,
            "nightly_at": nightly_at,
            "model": {
                "fitted":          self._model.is_fitted,
                "samples":         self._model.n_samples,
                "r2_pct":          round(self._model.r2 * 100.0, 1),
                "rmse_wh":         round(self._model.rmse, 1),
                "mae_wh":          round(self._model.mae, 1),
                "db_rows":         self._db.count(),
                "drift_detected":  drift_detected,
                "avg_accuracy_7d": avg_acc_7d,
            },
            "discovery": {
                "house_power_sensor":  d.house_power_sensor if d else None,
                "n_appliances":        len(d.appliance_sensors) if d else 0,
                "appliance_sensors":   d.appliance_sensors if d else [],
                "persons":             d.person_entities if d else [],
                "weather_entity":      d.weather_entity if d else None,
                "workday_sensor":      d.workday_sensor if d else None,
                "calendars_available": d.calendar_entities if d else [],
                "calendars_active":    self._calendar_ids,
                "holiday_dates":       sorted(self._holiday_dates),
            },
            "snapshot": await self.hass.async_add_executor_job(
                self._db.get_latest_daily_snapshot
            ),
            "accuracy": await self.hass.async_add_executor_job(
                self._db.get_latest_forecast_accuracy
            ),
            "updated_at": dt_util.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Power reading
    # ------------------------------------------------------------------

    def _read_house_power_w(self) -> float | None:
        """Return current house power in watts."""
        if self._house_power_sensor:
            st = self.hass.states.get(self._house_power_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try:
                    return float(st.state)
                except (ValueError, TypeError):
                    pass
            return None

        # Fallback: sum appliances
        total = 0.0
        valid = False
        for eid in self._appliance_sensors:
            st = self.hass.states.get(eid)
            if st and st.state not in ("unknown", "unavailable"):
                try:
                    total += float(st.state)
                    valid = True
                except (ValueError, TypeError):
                    pass
        return total if valid else None

    def _read_appliance_states(self) -> dict[str, float]:
        """Return {entity_id: watts} for all appliance sensors."""
        result: dict[str, float] = {}
        for eid in self._appliance_sensors:
            st = self.hass.states.get(eid)
            if st and st.state not in ("unknown", "unavailable"):
                try:
                    result[eid] = float(st.state)
                except (ValueError, TypeError):
                    pass
        return result

    # ------------------------------------------------------------------
    # Presence / workday
    # ------------------------------------------------------------------

    def _read_presence(self) -> dict[str, int]:
        presence: dict[str, int] = {}
        for pid in self._person_ids:
            st = self.hass.states.get(pid)
            presence[pid] = int(st is not None and st.state == "home")
        return presence

    def _read_workday(self) -> int:
        if self._workday_sensor:
            st = self.hass.states.get(self._workday_sensor)
            if st:
                return int(st.state == "on")
        # Fallback: Mon-Fri = workday
        return int(dt_util.now().weekday() < 5)

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------

    async def _read_current_weather(self) -> tuple[float, float]:
        if not self._weather_entity:
            return 15.0, 50.0
        st = self.hass.states.get(self._weather_entity)
        if st is None:
            return 15.0, 50.0
        a = st.attributes
        return float(a.get("temperature") or 15.0), float(a.get("cloud_coverage") or 50.0)

    async def _get_weather_forecast(self) -> list[dict]:
        now = dt_util.utcnow()
        if (
            self._wx_cache is not None
            and self._wx_cache_at is not None
            and now - self._wx_cache_at < _WEATHER_CACHE_TTL
        ):
            return self._wx_cache

        if not self._weather_entity:
            return []

        try:
            result = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": self._weather_entity, "type": "hourly"},
                blocking=True, return_response=True,
            )
            forecasts = []
            if isinstance(result, dict):
                forecasts = result.get(self._weather_entity, {}).get("forecast", [])
            self._wx_cache = forecasts
            self._wx_cache_at = now
            return forecasts
        except Exception as exc:
            _LOGGER.debug("Weather forecast unavailable: %s", exc)
            return self._wx_cache or []

    @staticmethod
    def _wx_lookup(forecasts: list[dict]) -> dict[str, tuple[float, float]]:
        lookup: dict[str, tuple[float, float]] = {}
        tz = dt_util.DEFAULT_TIME_ZONE
        for fc in forecasts:
            raw = fc.get("datetime", "")
            try:
                fc_dt = dt_util.parse_datetime(raw)
                if fc_dt:
                    local = fc_dt.astimezone(tz)
                    key = local.strftime("%Y-%m-%d %H")
                    lookup[key] = (
                        float(fc.get("temperature") or 15.0),
                        float(fc.get("cloud_coverage") or 50.0),
                    )
            except Exception:
                pass
        return lookup

    # ------------------------------------------------------------------
    # Calendar / holiday resolution
    # ------------------------------------------------------------------

    async def _get_holiday_dates(self) -> set[str]:
        """
        Query the configured calendar entities for events in the next 8 days
        (today + 7 forecast days).  Returns a set of YYYY-MM-DD strings where
        at least one event exists — those days are treated as non-workdays.

        Both all-day and timed events count.  Select only calendars that contain
        holidays/vacation; do NOT include e.g. a garbage-collection calendar.

        Silently returns an empty set when:
          • no calendars are configured
          • a configured entity does not exist in HA
          • the calendar.get_events service call fails (HA < 2022.5)
        """
        if not self._calendar_ids:
            return set()

        valid_ids = [
            eid for eid in self._calendar_ids
            if self.hass.states.get(eid) is not None
        ]
        if not valid_ids:
            _LOGGER.debug(
                "HCML: configured calendars not found in HA: %s", self._calendar_ids
            )
            return set()

        now   = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = start + timedelta(days=8)
        tz    = dt_util.DEFAULT_TIME_ZONE

        try:
            result = await self.hass.services.async_call(
                "calendar", "get_events",
                {
                    "entity_id":       valid_ids,
                    "start_date_time": start.isoformat(),
                    "end_date_time":   end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as exc:
            _LOGGER.debug("calendar.get_events unavailable: %s", exc)
            return set()

        dates: set[str] = set()
        if isinstance(result, dict):
            for cal_data in result.values():
                for event in (cal_data.get("events") or []):
                    raw = str(event.get("start") or "")
                    if len(raw) == 10:
                        # All-day event: "YYYY-MM-DD"
                        dates.add(raw)
                    elif raw:
                        # Timed event: parse to local date
                        try:
                            ev_dt = dt_util.parse_datetime(raw)
                            if ev_dt:
                                dates.add(ev_dt.astimezone(tz).strftime("%Y-%m-%d"))
                        except Exception:
                            pass

        if dates:
            _LOGGER.debug("Holiday/vacation dates from calendars: %s", sorted(dates))
        return dates

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    async def _collect_datapoint(self) -> None:
        assert self._db is not None
        now = dt_util.now()
        ts  = now.replace(minute=0, second=0, microsecond=0).isoformat()

        power_w = self._read_house_power_w()
        if power_w is None:
            _LOGGER.debug("House power unavailable — skipping data point")
            return
        if power_w < 0:
            _LOGGER.debug(
                "House power sensor reports %.0f W (negative = solar export) — skipping",
                power_w,
            )
            return

        # ── Plausibility checks ──────────────────────────────────────────
        if not (PLAUSIBILITY_MIN_W <= power_w <= PLAUSIBILITY_MAX_W):
            _LOGGER.warning(
                "House power %.0f W outside plausible range [%g–%g W] — skipping",
                power_w, PLAUSIBILITY_MIN_W, PLAUSIBILITY_MAX_W,
            )
            return

        recent = self._db.get_last_n_readings(6)
        if len(recent) >= 3:
            arr = np.array(recent, dtype=float)
            std = float(np.std(arr))
            if std > 50.0:
                med = float(np.median(arr))
                if abs(power_w - med) > OUTLIER_STD * 2.0 * std:
                    _LOGGER.warning(
                        "House power %.0f W looks like a spike "
                        "(median=%.0f W, σ=%.0f W) — skipping",
                        power_w, med, std,
                    )
                    return

        presence  = self._read_presence()
        devices   = self._read_appliance_states()
        today_str = now.strftime("%Y-%m-%d")
        is_wday   = 0 if today_str in self._holiday_dates else self._read_workday()
        temp, cld = await self._read_current_weather()

        self._db.insert_or_update(
            ts=ts,
            hour=now.hour,
            day_of_week=now.weekday(),
            month=now.month,
            is_workday=is_wday,
            temperature=temp,
            cloud_cover=cld,
            consumption_wh=power_w,   # W × 1 h ≈ Wh
            presence=presence,
            devices=devices,
        )
        _LOGGER.debug(
            "Stored %.0f Wh  persons=%s  devices=%d  wday=%s",
            power_w,
            {k.split(".")[-1]: v for k, v in presence.items()},
            len(devices),
            bool(is_wday),
        )

    # ------------------------------------------------------------------
    # Model training
    # ------------------------------------------------------------------

    def _train_model(self) -> None:
        assert self._db is not None
        rows = self._db.load_training_data(days=90)
        if len(rows) < MIN_SAMPLES:
            _LOGGER.debug("Not enough samples (%d) — skipping training", len(rows))
            return

        n_persons = len(self._person_ids)
        # Only subtract watts from the current (clean) appliance list so that
        # old rows with stale device entity-ids don't skew the base-load target.
        current_appliances = set(self._appliance_sensors)

        X_lst, y_lst = [], []
        for r in rows:
            devices    = r.get("devices") or {}
            total_wh   = r["consumption_wh"]
            # Base load = total consumption minus the known device watts.
            # Watts stored per-hour ≈ Wh for a 1-hour sample.
            device_wh  = sum(
                float(v)
                for eid, v in devices.items()
                if eid in current_appliances and isinstance(v, (int, float))
            )
            base_wh = max(0.0, total_wh - device_wh)

            feats = build_features(
                hour=r["hour"],
                day_of_week=r["day_of_week"],
                month=r["month"],
                is_workday=r["is_workday"],
                presence=r.get("presence") or {},
                n_persons_total=n_persons,
                temperature=float(r.get("temperature") or 15.0),
                cloud_cover=float(r.get("cloud_cover") or 50.0),
            )
            X_lst.append(feats)
            y_lst.append(base_wh)

        # Sample weights: exponential decay with 30-day half-life
        # so that recent data influences the model more than old bootstrap data
        weights_lst = []
        now_ts = datetime.utcnow()
        for r in rows:
            try:
                row_dt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                age_d  = max(
                    0.0,
                    (now_ts - row_dt.replace(tzinfo=None)).total_seconds() / 86400.0,
                )
            except Exception:
                age_d = 45.0  # fallback: middle of 90-day window
            weights_lst.append(float(np.exp(-age_d / 30.0)))

        X = np.array(X_lst,       dtype=float)
        y = np.array(y_lst,       dtype=float)
        W = np.array(weights_lst, dtype=float)

        # Remove outliers (apply same mask to weights)
        mean_y, std_y = np.mean(y), np.std(y)
        if std_y > 0:
            mask = np.abs(y - mean_y) <= OUTLIER_STD * std_y
            X, y, W = X[mask], y[mask], W[mask]

        # Normalise weights so Ridge alpha scale stays consistent
        if W.mean() > 0:
            W /= W.mean()

        self._model.fit(X, y, sample_weight=W)
        self._hourly_means      = self._db.get_hourly_means()
        self._presence_patterns = self._db.get_presence_patterns(self._person_ids)
        self._device_patterns   = self._db.get_device_patterns()

        _LOGGER.info(
            "Model trained (base load): %d samples | R²=%.1f%% | RMSE=%.0f Wh | MAE=%.0f Wh",
            self._model.n_samples,
            self._model.r2 * 100,
            self._model.rmse,
            self._model.mae,
        )

    # ------------------------------------------------------------------
    # Daily snapshot  (actual consumption, written once per day)
    # ------------------------------------------------------------------

    async def _maybe_save_snapshot(self) -> None:
        """
        Write a daily actual-consumption snapshot for *yesterday* if one does
        not yet exist.  Checked on every hourly update so it survives HA
        restarts, outages, and the case where the integration was first
        installed after midnight.
        """
        assert self._db is not None
        now       = dt_util.now()
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        exists = await self.hass.async_add_executor_job(
            self._db.has_daily_snapshot, yesterday
        )
        if exists:
            return

        rows = await self.hass.async_add_executor_job(
            self._db.get_rows_for_local_date, yesterday
        )
        if len(rows) < SNAPSHOT_MIN_HOURS:
            _LOGGER.debug(
                "Snapshot for %s skipped: only %d/%d hours available",
                yesterday, len(rows), SNAPSHOT_MIN_HOURS,
            )
            return

        by_hour  = {r["hour"]: r["consumption_wh"] for r in rows}
        hourly_wh = [by_hour.get(h) for h in range(24)]
        actual_kwh = round(
            sum(v for v in hourly_wh if v is not None) / 1000.0, 3
        )

        await self.hass.async_add_executor_job(
            self._db.save_daily_snapshot,
            yesterday,
            actual_kwh,
            hourly_wh,
            len(rows),
            dt_util.utcnow().isoformat(),
        )
        _LOGGER.info(
            "Snapshot saved for %s: %.2f kWh (%d/24h)",
            yesterday, actual_kwh, len(rows),
        )

    # ------------------------------------------------------------------
    # Morning forecast freeze + accuracy evaluation  (Plan 2)
    # ------------------------------------------------------------------

    async def _maybe_freeze_and_evaluate(self, days: list[dict]) -> None:
        """
        Two jobs per call:

        1. If the hour is between 00:00–05:59 and no freeze exists for today,
           persist the current forecast as today's immutable morning snapshot.

        2. If yesterday's freeze exists but accuracy hasn't been computed yet,
           and yesterday's actual snapshot is available, compute accuracy +
           device-level explanation and store it.
        """
        assert self._db is not None
        now       = dt_util.now()
        today     = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        # ── 1. Freeze today's forecast (only between 00:00 and 05:59) ────
        if now.hour < 6:
            exists = await self.hass.async_add_executor_job(
                self._db.has_forecast_freeze, today
            )
            if not exists and days:
                today_day = days[0]
                dow = now.weekday()
                device_predictions = {
                    eid: round(sum(
                        self._device_patterns.get((dow, h, eid), 0.0)
                        for h in range(24)
                    ))
                    for eid in self._appliance_sensors
                }
                # hourly_kwh stored as kWh → convert to Wh for storage
                await self.hass.async_add_executor_job(
                    self._db.save_forecast_freeze,
                    today,
                    dt_util.utcnow().isoformat(),
                    [round(v * 1000) for v in today_day["hourly_kwh"]],
                    [round(v * 1000) for v in today_day["hourly_base_kwh"]],
                    [round(v * 1000) for v in today_day["hourly_device_kwh"]],
                    device_predictions,
                )
                _LOGGER.info(
                    "Forecast frozen for %s at %02d:xx", today, now.hour
                )

        # ── 2. Compute accuracy for yesterday (if not yet done) ───────────
        freeze_row = await self.hass.async_add_executor_job(
            self._db.get_forecast_freeze, yesterday
        )
        if freeze_row is None or freeze_row.get("accuracy_pct") is not None:
            return  # No freeze, or already computed

        actual_snapshot = await self.hass.async_add_executor_job(
            self._db.get_daily_snapshot, yesterday
        )
        if actual_snapshot is None:
            return  # Actual data not available yet

        actual_rows = await self.hass.async_add_executor_job(
            self._db.get_rows_for_local_date, yesterday
        )

        # Resolve friendly names in async context (hass.states must not be
        # accessed from an executor thread)
        friendly_names: dict[str, str] = {}
        for eid in self._appliance_sensors:
            st = self.hass.states.get(eid)
            friendly_names[eid] = (
                st.attributes.get("friendly_name") if st else None
            ) or eid.split(".")[-1]

        accuracy_pct, delta_kwh, explanation = self._compute_accuracy_and_explanation(
            freeze_row, actual_snapshot, actual_rows, friendly_names
        )

        if accuracy_pct is not None:
            await self.hass.async_add_executor_job(
                self._db.update_forecast_accuracy,
                yesterday, accuracy_pct, delta_kwh, explanation,
            )
            _LOGGER.info(
                "Forecast accuracy for %s: %.1f%% (Δ=%.2f kWh, %d explanations)",
                yesterday, accuracy_pct, delta_kwh, len(explanation),
            )

    @staticmethod
    def _compute_accuracy_and_explanation(
        freeze_row: dict,
        actual_snapshot: dict,
        actual_rows: list[dict],
        friendly_names: dict[str, str],
    ) -> tuple[float | None, float, list[dict]]:
        """
        Pure computation (no I/O) — safe to run in async context.

        Returns (accuracy_pct, delta_kwh, explanation_list).
        accuracy_pct is None if not enough data to compute.
        """
        forecast_wh = freeze_row["hourly_wh"]
        actual_wh   = actual_snapshot["hourly_wh"]  # may contain None for missing hours

        # Only compare hours where actual measurement is available
        pairs = [
            (f or 0.0, a)
            for f, a in zip(forecast_wh, actual_wh)
            if a is not None
        ]
        if not pairs:
            return None, 0.0, []

        forecast_total = sum(f for f, a in pairs)
        actual_total   = sum(a for f, a in pairs)
        delta_wh  = actual_total - forecast_total
        delta_kwh = round(delta_wh / 1000.0, 3)
        accuracy  = (
            round(max(0.0, 100.0 * (1.0 - abs(delta_wh) / actual_total)), 1)
            if actual_total > 0 else None
        )

        # ── Device-level contribution to the delta ────────────────────────
        actual_device_wh: dict[str, float] = {}
        for row in actual_rows:
            for eid, w in (row.get("devices") or {}).items():
                try:
                    actual_device_wh[eid] = (
                        actual_device_wh.get(eid, 0.0) + float(w)
                    )
                except (TypeError, ValueError):
                    pass

        device_predictions = freeze_row.get("device_predictions") or {}
        explanation: list[dict] = []
        total_dev_delta = 0.0

        for eid in set(device_predictions) | set(actual_device_wh):
            predicted  = float(device_predictions.get(eid, 0.0))
            actual_d   = actual_device_wh.get(eid, 0.0)
            dev_delta  = actual_d - predicted
            total_dev_delta += dev_delta
            if abs(dev_delta) < 100:   # ignore changes < 100 Wh
                continue
            name      = friendly_names.get(eid, eid.split(".")[-1])
            direction = (
                "unerwartet aktiv"
                if dev_delta > 0
                else "weniger aktiv als erwartet"
            )
            explanation.append({
                "entity":    eid,
                "name":      name,
                "delta_wh":  round(dev_delta),
                "delta_kwh": round(dev_delta / 1000.0, 3),
                "direction": direction,
                "label": (
                    f"{name} {'+' if dev_delta > 0 else ''}"
                    f"{dev_delta / 1000.0:.2f} kWh ({direction})"
                ),
            })

        # Sort by magnitude, keep top 5
        explanation.sort(key=lambda x: abs(x["delta_wh"]), reverse=True)
        explanation = explanation[:5]

        # ── Base-load residual ────────────────────────────────────────────
        base_delta = delta_wh - total_dev_delta
        if abs(base_delta) > 100:
            direction = (
                "höher als erwartet"
                if base_delta > 0
                else "niedriger als erwartet"
            )
            explanation.append({
                "entity":    "base_load",
                "name":      "Grundlast",
                "delta_wh":  round(base_delta),
                "delta_kwh": round(base_delta / 1000.0, 3),
                "direction": direction,
                "label": (
                    f"Grundlast {'+' if base_delta > 0 else ''}"
                    f"{base_delta / 1000.0:.2f} kWh ({direction})"
                ),
            })

        return accuracy, delta_kwh, explanation

    # ------------------------------------------------------------------
    # Drift detection
    # ------------------------------------------------------------------

    def _check_drift(self) -> tuple[bool, float | None]:
        """
        Compare the rolling 7-day average forecast accuracy against the
        warning threshold.  Returns (drift_detected, avg_accuracy_pct_or_None).
        Runs in executor (SQLite I/O).
        """
        assert self._db is not None
        recent = self._db.get_recent_forecast_accuracies(7)
        if len(recent) < DRIFT_MIN_DAYS:
            return False, None
        avg   = sum(r["accuracy_pct"] for r in recent) / len(recent)
        drift = avg < DRIFT_WARNING_THRESHOLD_PCT
        if drift:
            _LOGGER.warning(
                "HCML drift detected: avg forecast accuracy = %.1f%% over %d days "
                "(threshold %.1f%%) — check sensors or consider model reset",
                avg, len(recent), DRIFT_WARNING_THRESHOLD_PCT,
            )
        return drift, round(avg, 1)

    # ------------------------------------------------------------------
    # Nightly forecast refresh  (Plan 3)
    # ------------------------------------------------------------------

    async def _maybe_refresh_nightly_forecast(self) -> None:
        """
        Build and persist the 7-day forecast once per night (00:00–05:59).
        Skipped if a forecast for the current calendar date already exists.
        """
        assert self._db is not None
        today  = dt_util.now().strftime("%Y-%m-%d")
        latest = await self.hass.async_add_executor_job(
            self._db.get_latest_nightly_forecast
        )
        if latest and latest["created_date"] == today:
            return  # Already built for this night

        forecast = await self._build_forecast()
        await self.hass.async_add_executor_job(
            self._db.save_nightly_forecast,
            today,
            dt_util.utcnow().isoformat(),
            forecast["days"],
            forecast["total_kwh"],
        )
        _LOGGER.info(
            "Nightly forecast saved for %s: %.1f kWh (7 days)",
            today, forecast["total_kwh"],
        )

    # ------------------------------------------------------------------
    # Forecast builder  (pure calculation — no DB reads/writes here)
    # ------------------------------------------------------------------

    async def _build_forecast(self) -> dict[str, Any]:
        now       = dt_util.now()
        wx_list   = await self._get_weather_forecast()
        wx_map    = self._wx_lookup(wx_list)

        presence_now = self._read_presence()
        wday_now     = self._read_workday()
        devices_now  = self._read_appliance_states()

        n_persons = len(self._person_ids)

        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        days: list[dict] = []

        for day_idx in range(FORECAST_DAYS):
            day_start = (start + timedelta(days=day_idx)).replace(hour=0)
            is_holiday = day_start.strftime("%Y-%m-%d") in self._holiday_dates
            hourly_kwh: list[float]        = []
            hourly_base_kwh: list[float]   = []
            hourly_device_kwh: list[float] = []
            daily_wh = 0.0

            for hour in range(24):
                fc_dt  = day_start + timedelta(hours=hour)
                wx_key = fc_dt.strftime("%Y-%m-%d %H")
                temp, cloud = wx_map.get(wx_key, (15.0, 50.0))
                dow = fc_dt.weekday()

                if day_idx == 0:
                    presence = presence_now
                    wday     = 0 if is_holiday else wday_now
                else:
                    presence = {
                        pid: int(
                            self._presence_patterns.get((dow, hour, pid), 0.4) >= 0.5
                        )
                        for pid in self._person_ids
                    }
                    wday = 0 if is_holiday else int(dow < 5)

                feats = build_features(
                    hour=fc_dt.hour,
                    day_of_week=dow,
                    month=fc_dt.month,
                    is_workday=wday,
                    presence=presence,
                    n_persons_total=n_persons,
                    temperature=temp,
                    cloud_cover=cloud,
                )

                # Base load predicted by Ridge (fridge, standby, heating, etc.)
                if self._model.is_fitted:
                    base_wh = self._model.predict_one(feats)
                else:
                    total_mean = self._hourly_means.get((dow, hour), 500.0)
                    device_mean = sum(
                        self._device_patterns.get((dow, hour, eid), 0.0)
                        for eid in self._appliance_sensors
                    )
                    base_wh = max(0.0, total_mean - device_mean)

                # Device contribution: historically learned average wattage
                # per (day_of_week, hour, device).
                device_wh = sum(
                    self._device_patterns.get((dow, hour, eid), 0.0)
                    for eid in self._appliance_sensors
                )

                total_wh = base_wh + device_wh
                hourly_kwh.append(round(total_wh / 1000.0, 3))
                hourly_base_kwh.append(round(base_wh / 1000.0, 3))
                hourly_device_kwh.append(round(device_wh / 1000.0, 3))
                daily_wh += total_wh

            days.append({
                "date":              day_start.strftime("%Y-%m-%d"),
                "day_name":          day_start.strftime("%A"),
                "predicted_kwh":     round(daily_wh / 1000.0, 2),
                "base_kwh":          round(sum(hourly_base_kwh), 2),
                "device_kwh":        round(sum(hourly_device_kwh), 2),
                "hourly_kwh":        hourly_kwh,
                "hourly_base_kwh":   hourly_base_kwh,
                "hourly_device_kwh": hourly_device_kwh,
            })

        return {
            "days":      days,
            "total_kwh": round(sum(x["predicted_kwh"] for x in days), 2),
        }

    # ------------------------------------------------------------------
    # Bootstrap from SFML solar_forecast.db
    # ------------------------------------------------------------------

    def _bootstrap_from_sfml_db(self, sfml_path: str) -> int:
        """
        Read house consumption history directly from SFML's SQLite database.
        Runs in executor (blocking I/O).

        Returns the number of rows imported (0 = DB not found or no usable data).
        """
        try:
            conn = sqlite3.connect(f"file:{sfml_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            _LOGGER.debug("SFML DB not found at %s — skipping", sfml_path)
            return 0

        try:
            conn.row_factory = sqlite3.Row

            # 1. Log full schema so we can see what's available
            tables = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()

            if not tables:
                _LOGGER.warning("SFML DB is empty — no tables found")
                return 0

            _LOGGER.info("=== SFML DB schema at %s ===", sfml_path)
            for t in tables:
                _LOGGER.info("TABLE %s:\n%s", t["name"], t["sql"])

            # 2. Log row counts per table
            for t in tables:
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM [{t['name']}]"  # noqa: S608
                    ).fetchone()[0]
                    _LOGGER.info("  %s: %d rows", t["name"], count)
                except Exception:
                    pass

            # 3. Try to find and import house consumption data
            imported = self._import_sfml_consumption(conn, tables)
            _LOGGER.info("SFML bootstrap: %d rows imported", imported)
            return imported

        except Exception as exc:
            _LOGGER.warning("SFML DB read error: %s", exc, exc_info=True)
            return 0
        finally:
            conn.close()

    def _import_sfml_consumption(self, conn: sqlite3.Connection, tables: list) -> int:
        """
        Heuristically find the house consumption column and import it.

        We look for columns whose name contains 'house', 'verbrauch',
        'consumption', 'load' alongside a timestamp column.
        """
        tz = dt_util.DEFAULT_TIME_ZONE
        imported = 0

        # Keywords that indicate house consumption (watts or Wh)
        CONSUMPTION_KW = ("house", "verbrauch", "consumption", "load", "haushalt")
        TIMESTAMP_KW   = ("timestamp", "ts", "datetime", "date", "time", "zeit")

        for table in tables:
            tname = table["name"]
            sql   = (table["sql"] or "").lower()

            # Quick filter: table must mention both a time-like and consumption-like column
            if not any(k in sql for k in CONSUMPTION_KW):
                continue
            if not any(k in sql for k in TIMESTAMP_KW):
                continue

            # Get column names
            try:
                cols = [
                    row[1].lower()
                    for row in conn.execute(f"PRAGMA table_info([{tname}])").fetchall()
                ]
            except Exception:
                continue

            ts_col  = next((c for c in cols if any(k in c for k in TIMESTAMP_KW)), None)
            wh_col  = next((c for c in cols if any(k in c for k in CONSUMPTION_KW)), None)

            if not ts_col or not wh_col:
                continue

            _LOGGER.info(
                "SFML import: using table=%s  ts=%s  consumption=%s",
                tname, ts_col, wh_col,
            )

            # Optional context columns
            def _find(keywords):
                return next((c for c in cols if any(k in c for k in keywords)), None)

            temp_col  = _find(("temp", "temperatur"))
            cloud_col = _find(("cloud", "wolke", "bedeckung"))

            cutoff = (datetime.utcnow() - timedelta(days=BOOTSTRAP_DAYS)).isoformat()

            try:
                rows = conn.execute(
                    f"SELECT * FROM [{tname}] WHERE [{ts_col}] > ? ORDER BY [{ts_col}]",  # noqa: S608
                    (cutoff,),
                ).fetchall()
            except Exception as exc:
                _LOGGER.debug("Query failed on %s: %s", tname, exc)
                continue

            for row in rows:
                row = dict(row)
                raw_wh = row.get(wh_col) or row.get(wh_col.upper())
                raw_ts = row.get(ts_col)  or row.get(ts_col.upper())
                if raw_wh is None or raw_ts is None:
                    continue
                try:
                    wh = float(raw_wh)
                    if wh <= 0:
                        continue
                    ts_dt = dt_util.parse_datetime(str(raw_ts))
                    if ts_dt is None:
                        # Try as Unix timestamp
                        ts_dt = datetime.utcfromtimestamp(float(raw_ts)).replace(
                            tzinfo=dt_util.UTC
                        )
                    local_dt = ts_dt.astimezone(tz)
                    self._db.insert_if_missing(
                        ts=ts_dt.isoformat(),
                        hour=local_dt.hour,
                        day_of_week=local_dt.weekday(),
                        month=local_dt.month,
                        temperature=float(row.get(temp_col, 15.0) or 15.0) if temp_col else 15.0,
                        cloud_cover=float(row.get(cloud_col, 50.0) or 50.0) if cloud_col else 50.0,
                        consumption_wh=wh,
                    )
                    imported += 1
                except Exception:
                    continue

            if imported > 0:
                break  # One good table is enough

        return imported

    # ------------------------------------------------------------------
    # Bootstrap from HA recorder
    # ------------------------------------------------------------------

    async def _bootstrap_from_recorder(self) -> None:
        assert self._db is not None
        _LOGGER.info("Bootstrapping from HA recorder (last %d days)…", BOOTSTRAP_DAYS)

        target = self._house_power_sensor or (
            self._appliance_sensors[0] if self._appliance_sensors else None
        )
        if target is None:
            _LOGGER.warning("Bootstrap: no sensor to import — skipping")
            return

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            start = dt_util.utcnow() - timedelta(days=BOOTSTRAP_DAYS)
            tz    = dt_util.DEFAULT_TIME_ZONE

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass, start, None,
                {target}, "hour", None, {"mean"},
            )

            # Build row list in the event loop (pure computation, no I/O)
            rows: list[dict] = []
            for stat in stats.get(target, []):
                mean_w = stat.get("mean") or 0.0
                if mean_w <= 0:
                    continue
                raw = stat["start"]
                if isinstance(raw, (int, float)):
                    utc_dt = datetime.utcfromtimestamp(raw).replace(tzinfo=dt_util.UTC)
                else:
                    utc_dt = raw
                local_dt = utc_dt.astimezone(tz)
                rows.append({
                    "ts":             utc_dt.isoformat(),
                    "hour":           local_dt.hour,
                    "day_of_week":    local_dt.weekday(),
                    "month":          local_dt.month,
                    "consumption_wh": float(mean_w),
                })

            # Batch-insert in executor — one connection, one transaction, non-blocking
            imported = await self.hass.async_add_executor_job(
                self._db.insert_many_if_missing, rows
            )

            _LOGGER.info("Bootstrap complete: %d records imported from %s", imported, target)

        except ImportError:
            _LOGGER.warning("HA recorder not available — starting without history")
        except Exception as exc:
            _LOGGER.warning("Bootstrap error: %s", exc, exc_info=True)
