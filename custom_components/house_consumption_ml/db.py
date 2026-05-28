"""SQLite persistence layer for House Consumption ML."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = [
    # ------------------------------------------------------------------ #
    # Core hourly measurement history                                     #
    # ------------------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS consumption_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             TEXT    NOT NULL UNIQUE,   -- ISO-8601 local with tz offset, hour-truncated
        hour           INTEGER NOT NULL,           -- 0-23 local
        day_of_week    INTEGER NOT NULL,           -- 0=Mon…6=Sun local
        month          INTEGER NOT NULL,           -- 1-12 local
        is_workday     INTEGER NOT NULL DEFAULT 0,
        temperature    REAL    DEFAULT 15.0,       -- °C
        cloud_cover    REAL    DEFAULT 50.0,       -- 0-100
        consumption_wh REAL    NOT NULL,           -- Wh this hour
        presence_json  TEXT    DEFAULT '{}',       -- {"person.daniel": 1, …}
        devices_json   TEXT    DEFAULT '{}'        -- {"sensor.shelly_tv": 5.8, …}
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ts  ON consumption_history(ts)",
    "CREATE INDEX IF NOT EXISTS idx_dow ON consumption_history(day_of_week, hour)",

    # ------------------------------------------------------------------ #
    # Daily actuals snapshot (written once per day, around midnight)      #
    # ------------------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS daily_snapshots (
        date           TEXT    PRIMARY KEY,        -- YYYY-MM-DD local
        actual_kwh     REAL    NOT NULL,            -- Day total kWh
        hourly_wh_json TEXT    NOT NULL,            -- JSON [wh_0..wh_23], null for missing hours
        hours_count    INTEGER NOT NULL,            -- Available hours out of 24
        recorded_at    TEXT    NOT NULL             -- ISO-8601 UTC when written
    )
    """,

    # ------------------------------------------------------------------ #
    # Morning forecast freeze + accuracy (Plan 2)                         #
    # Written once per day between 00:00–05:59 local time                #
    # accuracy_pct / delta_kwh / explanation_json filled in next morning  #
    # ------------------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS forecast_snapshots (
        date                    TEXT    PRIMARY KEY,  -- YYYY-MM-DD local
        frozen_at               TEXT    NOT NULL,      -- ISO-8601 UTC
        hourly_wh_json          TEXT    NOT NULL,      -- [wh_0..wh_23] total predicted (Wh)
        hourly_base_wh_json     TEXT    NOT NULL,      -- base-load component
        hourly_device_wh_json   TEXT    NOT NULL,      -- device-pattern component
        device_predictions_json TEXT    NOT NULL,      -- {entity_id: predicted_wh_for_day}
        accuracy_pct            REAL,                  -- NULL until actual data available
        delta_kwh               REAL,                  -- actual_kwh - forecast_kwh
        explanation_json        TEXT                   -- JSON list, NULL until computed
    )
    """,

    # ------------------------------------------------------------------ #
    # Nightly forecast freeze (Plan 3)                                     #
    # Built once per night (00:00–05:59) — sensors read from this table   #
    # One row per night; INSERT OR REPLACE overwrites previous same-night  #
    # ------------------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS nightly_forecast (
        created_date TEXT    PRIMARY KEY,  -- YYYY-MM-DD local (which night)
        created_at   TEXT    NOT NULL,      -- ISO-8601 UTC
        days_json    TEXT    NOT NULL,      -- JSON list of 7 day dicts (same as coordinator days[])
        total_kwh    REAL    NOT NULL
    )
    """,
]

# Migrations: add columns that didn't exist in v1.0
_MIGRATIONS = [
    "ALTER TABLE consumption_history ADD COLUMN presence_json TEXT DEFAULT '{}'",
    "ALTER TABLE consumption_history ADD COLUMN devices_json  TEXT DEFAULT '{}'",
]

_INSERT_OR_UPDATE = """
INSERT INTO consumption_history
    (ts, hour, day_of_week, month, is_workday,
     temperature, cloud_cover, consumption_wh, presence_json, devices_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ts) DO UPDATE SET
    consumption_wh = excluded.consumption_wh,
    is_workday     = excluded.is_workday,
    temperature    = excluded.temperature,
    cloud_cover    = excluded.cloud_cover,
    presence_json  = excluded.presence_json,
    devices_json   = excluded.devices_json
"""

_INSERT_IF_MISSING = """
INSERT OR IGNORE INTO consumption_history
    (ts, hour, day_of_week, month, is_workday,
     temperature, cloud_cover, consumption_wh, presence_json, devices_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class HCMLDatabase:

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._init()

    # ------------------------------------------------------------------
    # Setup / migration
    # ------------------------------------------------------------------

    def _init(self) -> None:
        with sqlite3.connect(self._path) as conn:
            for stmt in _DDL:
                conn.execute(stmt)
            # Apply migrations idempotently
            for mig in _MIGRATIONS:
                try:
                    conn.execute(mig)
                except sqlite3.OperationalError:
                    pass  # Column already exists
            conn.commit()
        _LOGGER.debug("DB ready: %s", self._path)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _row(self, kw: dict) -> tuple:
        return (
            kw["ts"],
            kw["hour"],
            kw["day_of_week"],
            kw["month"],
            kw.get("is_workday", 0),
            kw.get("temperature", 15.0),
            kw.get("cloud_cover", 50.0),
            kw["consumption_wh"],
            json.dumps(kw.get("presence", {})),
            json.dumps(kw.get("devices", {})),
        )

    def insert_or_update(self, **kw) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(_INSERT_OR_UPDATE, self._row(kw))
            conn.commit()

    def insert_if_missing(self, **kw) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(_INSERT_IF_MISSING, self._row(kw))
            conn.commit()

    def insert_many_if_missing(self, rows: list[dict]) -> int:
        """Batch-insert rows, ignoring duplicates. One connection for all rows.
        Returns the number of rows actually inserted."""
        with sqlite3.connect(self._path) as conn:
            inserted = 0
            for kw in rows:
                cursor = conn.execute(_INSERT_IF_MISSING, self._row(kw))
                inserted += cursor.rowcount
            conn.commit()
        return inserted

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def count(self) -> int:
        with sqlite3.connect(self._path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM consumption_history"
            ).fetchone()[0]

    def load_training_data(self, days: int = 90) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM consumption_history WHERE ts > ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["presence"] = json.loads(d.get("presence_json") or "{}")
            d["devices"]  = json.loads(d.get("devices_json")  or "{}")
            result.append(d)
        return result

    def get_hourly_means(self) -> dict[tuple[int, int], float]:
        """Fallback table: mean Wh per (day_of_week, hour)."""
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                """
                SELECT day_of_week, hour, AVG(consumption_wh) as m
                FROM consumption_history
                GROUP BY day_of_week, hour
                """
            ).fetchall()
        return {(r[0], r[1]): float(r[2]) for r in rows}

    def get_device_patterns(self) -> dict[tuple[int, int, str], float]:
        """
        Average wattage per (day_of_week, hour, device_entity_id) from stored data.

        Used to estimate future device load when actual states are unknown.
        Returns {(dow, hour, entity_id): avg_watts}.
        """
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT day_of_week, hour, devices_json FROM consumption_history"
            ).fetchall()

        buckets: dict[tuple[int, int, str], list[float]] = {}
        for row in rows:
            dow, hour, blob = row
            try:
                devices = json.loads(blob or "{}")
            except Exception:
                continue
            for eid, watt in devices.items():
                try:
                    buckets.setdefault((dow, hour, eid), []).append(float(watt))
                except (TypeError, ValueError):
                    pass

        return {k: sum(v) / len(v) for k, v in buckets.items()}

    def get_presence_patterns(
        self, person_ids: list[str]
    ) -> dict[tuple[int, int, str], float]:
        """
        Average presence probability per (day_of_week, hour, person_id).
        Derived from the stored presence_json blobs.
        """
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT day_of_week, hour, presence_json FROM consumption_history"
            ).fetchall()

        buckets: dict[tuple[int, int, str], list[float]] = {}
        for row in rows:
            dow, hour, blob = row
            presence = json.loads(blob or "{}")
            for pid in person_ids:
                key = (dow, hour, pid)
                buckets.setdefault(key, []).append(float(presence.get(pid, 0)))

        return {k: sum(v) / len(v) for k, v in buckets.items()}

    # ------------------------------------------------------------------
    # Daily snapshots  (actual consumption, written once per day)
    # ------------------------------------------------------------------

    def get_rows_for_local_date(self, local_date: str) -> list[dict]:
        """
        Return all consumption_history rows whose ts falls on `local_date`.

        The ts column stores ISO-8601 timestamps with the local timezone offset
        (e.g. "2026-05-26T14:00:00+02:00"), so a simple LIKE prefix filter on
        the date part is correct and fast via the idx_ts index.

        Args:
            local_date: "YYYY-MM-DD"
        """
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT hour, consumption_wh FROM consumption_history "
                "WHERE ts LIKE ? ORDER BY hour",
                (local_date + "T%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_daily_snapshot(self, date: str) -> bool:
        """Return True if a snapshot for `date` (YYYY-MM-DD) already exists."""
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_snapshots WHERE date = ?", (date,)
            ).fetchone()
        return row is not None

    def save_daily_snapshot(
        self,
        date: str,
        actual_kwh: float,
        hourly_wh: list,
        hours_count: int,
        recorded_at: str,
    ) -> None:
        """
        Persist a daily actual snapshot.  INSERT OR IGNORE — the first write
        for a given date wins; subsequent calls are silently skipped.
        """
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO daily_snapshots
                    (date, actual_kwh, hourly_wh_json, hours_count, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (date, actual_kwh, json.dumps(hourly_wh), hours_count, recorded_at),
            )
            conn.commit()

    def get_latest_daily_snapshot(self) -> dict | None:
        """Return the most recent daily snapshot as a dict, or None."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["hourly_wh"] = json.loads(d.pop("hourly_wh_json") or "[]")
        return d

    def get_daily_snapshot(self, date: str) -> dict | None:
        """Return the daily snapshot for a specific date (YYYY-MM-DD), or None."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM daily_snapshots WHERE date = ?", (date,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["hourly_wh"] = json.loads(d.pop("hourly_wh_json") or "[]")
        return d

    # ------------------------------------------------------------------
    # Forecast freeze + accuracy  (Plan 2)
    # ------------------------------------------------------------------

    def has_forecast_freeze(self, date: str) -> bool:
        """Return True if a forecast freeze for `date` (YYYY-MM-DD) already exists."""
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT 1 FROM forecast_snapshots WHERE date = ?", (date,)
            ).fetchone()
        return row is not None

    def save_forecast_freeze(
        self,
        date: str,
        frozen_at: str,
        hourly_wh: list,
        hourly_base_wh: list,
        hourly_device_wh: list,
        device_predictions: dict,
    ) -> None:
        """
        Persist a morning forecast freeze.  INSERT OR IGNORE — idempotent.
        hourly_wh / hourly_base_wh / hourly_device_wh are lists of Wh values.
        device_predictions is {entity_id: predicted_wh_for_day}.
        """
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO forecast_snapshots
                    (date, frozen_at, hourly_wh_json, hourly_base_wh_json,
                     hourly_device_wh_json, device_predictions_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    date,
                    frozen_at,
                    json.dumps(hourly_wh),
                    json.dumps(hourly_base_wh),
                    json.dumps(hourly_device_wh),
                    json.dumps(device_predictions),
                ),
            )
            conn.commit()

    def get_forecast_freeze(self, date: str) -> dict | None:
        """Return the forecast freeze for `date` as a dict, or None."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM forecast_snapshots WHERE date = ?", (date,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["hourly_wh"]          = json.loads(d.pop("hourly_wh_json")          or "[]")
        d["hourly_base_wh"]     = json.loads(d.pop("hourly_base_wh_json")     or "[]")
        d["hourly_device_wh"]   = json.loads(d.pop("hourly_device_wh_json")   or "[]")
        d["device_predictions"] = json.loads(d.pop("device_predictions_json") or "{}")
        d["explanation"]        = json.loads(d.pop("explanation_json")         or "[]")
        return d

    def update_forecast_accuracy(
        self,
        date: str,
        accuracy_pct: float,
        delta_kwh: float,
        explanation: list,
    ) -> None:
        """Fill in accuracy metrics for an existing forecast freeze row."""
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                UPDATE forecast_snapshots
                SET accuracy_pct = ?, delta_kwh = ?, explanation_json = ?
                WHERE date = ?
                """,
                (accuracy_pct, delta_kwh, json.dumps(explanation), date),
            )
            conn.commit()

    def get_latest_forecast_accuracy(self) -> dict | None:
        """Return the most recent forecast freeze that has accuracy computed, or None."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM forecast_snapshots
                WHERE accuracy_pct IS NOT NULL
                ORDER BY date DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["hourly_wh"]          = json.loads(d.pop("hourly_wh_json")          or "[]")
        d["hourly_base_wh"]     = json.loads(d.pop("hourly_base_wh_json")     or "[]")
        d["hourly_device_wh"]   = json.loads(d.pop("hourly_device_wh_json")   or "[]")
        d["device_predictions"] = json.loads(d.pop("device_predictions_json") or "{}")
        d["explanation"]        = json.loads(d.pop("explanation_json")         or "[]")
        return d

    # ------------------------------------------------------------------
    # Nightly forecast  (Plan 3 — stable sensor display)
    # ------------------------------------------------------------------

    def save_nightly_forecast(
        self,
        created_date: str,
        created_at: str,
        days: list,
        total_kwh: float,
    ) -> None:
        """
        Persist the nightly 7-day forecast.  INSERT OR REPLACE — one row per
        night; re-running in the same night window overwrites the previous value.
        """
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO nightly_forecast
                    (created_date, created_at, days_json, total_kwh)
                VALUES (?, ?, ?, ?)
                """,
                (created_date, created_at, json.dumps(days), total_kwh),
            )
            conn.commit()

    def get_latest_nightly_forecast(self) -> dict | None:
        """Return the most recently stored nightly forecast, or None."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM nightly_forecast ORDER BY created_date DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["days"] = json.loads(d.pop("days_json") or "[]")
        return d
