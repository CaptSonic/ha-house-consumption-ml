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
    """
    CREATE TABLE IF NOT EXISTS consumption_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             TEXT    NOT NULL UNIQUE,   -- ISO-8601 UTC, hour-truncated
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
