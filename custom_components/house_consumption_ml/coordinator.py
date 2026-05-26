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
    FORECAST_DAYS,
    LARGE_DEVICE_THRESHOLD_W,
    MIN_SAMPLES,
    OUTLIER_STD,
    RIDGE_ALPHA,
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
        await self._collect_datapoint()
        await self.hass.async_add_executor_job(self._train_model)
        return await self._build_forecast()

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

        presence  = self._read_presence()
        devices   = self._read_appliance_states()
        is_wday   = self._read_workday()
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

        X = np.array(X_lst, dtype=float)
        y = np.array(y_lst, dtype=float)

        # Remove outliers
        mean_y, std_y = np.mean(y), np.std(y)
        if std_y > 0:
            mask = np.abs(y - mean_y) <= OUTLIER_STD * std_y
            X, y = X[mask], y[mask]

        self._model.fit(X, y)
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
    # Forecast
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
                    wday     = wday_now
                else:
                    presence = {
                        pid: int(
                            self._presence_patterns.get((dow, hour, pid), 0.4) >= 0.5
                        )
                        for pid in self._person_ids
                    }
                    wday = int(dow < 5)

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
                "date":             day_start.strftime("%Y-%m-%d"),
                "day_name":         day_start.strftime("%A"),
                "predicted_kwh":    round(daily_wh / 1000.0, 2),
                "base_kwh":         round(sum(hourly_base_kwh), 2),
                "device_kwh":       round(sum(hourly_device_kwh), 2),
                "hourly_kwh":       hourly_kwh,
                "hourly_base_kwh":  hourly_base_kwh,
                "hourly_device_kwh": hourly_device_kwh,
            })

        d = self._discovery
        return {
            "days":      days,
            "total_kwh": round(sum(x["predicted_kwh"] for x in days), 2),
            "model": {
                "fitted":    self._model.is_fitted,
                "samples":   self._model.n_samples,
                "r2_pct":    round(self._model.r2 * 100.0, 1),
                "rmse_wh":   round(self._model.rmse, 1),
                "mae_wh":    round(self._model.mae, 1),
                "db_rows":   self._db.count() if self._db else 0,
            },
            "discovery": {
                "house_power_sensor": d.house_power_sensor if d else None,
                "n_appliances":       len(d.appliance_sensors) if d else 0,
                "appliance_sensors":  d.appliance_sensors if d else [],
                "persons":            d.person_entities if d else [],
                "weather_entity":     d.weather_entity if d else None,
                "workday_sensor":     d.workday_sensor if d else None,
            },
            "updated_at": dt_util.now().isoformat(),
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
