import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    endpoint TEXT NOT NULL,
    deployment TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    ttft_threshold_pct REAL,
    baseline_manual_ms REAL,
    baseline_reset_at REAL,
    token_param TEXT NOT NULL DEFAULT 'max_completion_tokens',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_check_at REAL,
    next_run_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    cycle_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    ts REAL NOT NULL,
    success INTEGER NOT NULL,
    http_status INTEGER,
    ttft_ms REAL,
    total_ms REAL,
    anomaly INTEGER NOT NULL DEFAULT 0,
    baseline_ms REAL,
    error TEXT,
    concurrent INTEGER,
    interfered INTEGER NOT NULL DEFAULT 0,
    exclusive INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checks_model_ts ON checks(model_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER REFERENCES models(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    email_status TEXT,
    feishu_status TEXT,
    notice_json TEXT,
    email_tries INTEGER NOT NULL DEFAULT 0,
    email_retry_at REAL,
    feishu_tries INTEGER NOT NULL DEFAULT 0,
    feishu_retry_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            check_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(checks)")}
            for col, ddl in (
                ("concurrent", "INTEGER"),
                ("interfered", "INTEGER NOT NULL DEFAULT 0"),
                ("exclusive", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in check_cols:
                    self._conn.execute(f"ALTER TABLE checks ADD COLUMN {col} {ddl}")
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
            for col, ddl in (
                ("feishu_status", "TEXT"),
                ("notice_json", "TEXT"),
                ("email_tries", "INTEGER NOT NULL DEFAULT 0"),
                ("email_retry_at", "REAL"),
                ("feishu_tries", "INTEGER NOT NULL DEFAULT 0"),
                ("feishu_retry_at", "REAL"),
            ):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {ddl}")
            for k, v in DEFAULT_SETTINGS.items():
                self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ---- settings ----
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.all("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, str]) -> None:
        with self._lock:
            for k, v in values.items():
                self._conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)),
                )

    # ---- models ----
    def get_model(self, model_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM models WHERE id = ?", (model_id,))

    def list_models(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM models ORDER BY id")

    def due_models(self, now: float) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM models WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)", (now,)
        )

    def insert_model(self, fields: dict[str, Any]) -> int:
        fields = {**fields, "created_at": time.time()}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(f"INSERT INTO models ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def update_model(self, model_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE models SET {sets} WHERE id = ?", (*fields.values(), model_id))

    def delete_model(self, model_id: int) -> None:
        self.execute("DELETE FROM models WHERE id = ?", (model_id,))

    # ---- checks ----
    def insert_check(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO checks ({cols}) VALUES ({marks})", tuple(row.values()))

    def list_checks(self, model_id: int, since: float, limit: int = 5000) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM checks WHERE model_id = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (model_id, since, limit),
        )

    def latest_check(self, model_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM checks WHERE model_id = ? ORDER BY ts DESC, id DESC LIMIT 1", (model_id,))

    def auto_baseline(self, model: dict[str, Any], window: int, min_samples: int) -> tuple[float | None, int]:
        """Median TTFT of the most recent clean checks: successful, not anomalous, not overlapping an image probe."""
        rows = self.all(
            "SELECT ttft_ms FROM checks WHERE model_id = ? AND success = 1 AND anomaly = 0 AND interfered = 0 "
            "AND ttft_ms IS NOT NULL AND ts > ? ORDER BY ts DESC LIMIT ?",
            (model["id"], model.get("baseline_reset_at") or 0, window),
        )
        values = [r["ttft_ms"] for r in rows]
        if len(values) < min_samples:
            return None, len(values)
        return float(statistics.median(values)), len(values)

    def availability(self, model_id: int, since: float) -> tuple[float | None, int]:
        """Share of check cycles (scheduled + manual) that eventually succeeded."""
        row = self.one(
            "SELECT COUNT(*) AS n, SUM(ok) AS up FROM ("
            " SELECT MAX(success) AS ok FROM checks WHERE model_id = ? AND ts >= ? GROUP BY cycle_id)",
            (model_id, since),
        )
        if not row or not row["n"]:
            return None, 0
        return row["up"] / row["n"] * 100, row["n"]

    def purge_older_than(self, cutoff: float) -> None:
        self.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))

    # ---- alerts ----
    def insert_alert(self, model_id: int | None, kind: str, subject: str, message: str, notice_json: str = "") -> int:
        cur = self.execute(
            "INSERT INTO alerts (model_id, ts, kind, subject, message, email_status, feishu_status, notice_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (model_id, time.time(), kind, subject, message, "发送中", "发送中", notice_json or None),
        )
        return int(cur.lastrowid)

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))

    def update_alert(self, alert_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE alerts SET {sets} WHERE id = ?", (*fields.values(), alert_id))

    def set_alert_status(self, alert_id: int, email_status: str, feishu_status: str) -> None:
        self.update_alert(alert_id, {"email_status": email_status, "feishu_status": feishu_status})

    def due_alert_retries(self, now: float) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM alerts WHERE email_retry_at <= ? OR feishu_retry_at <= ? ORDER BY ts, id", (now, now)
        )

    def pending_alerts(self, model_id: int, exclude_id: int) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM alerts WHERE model_id = ? AND id != ? "
            "AND (email_retry_at IS NOT NULL OR feishu_retry_at IS NOT NULL) ORDER BY ts, id",
            (model_id, exclude_id),
        )

    def expedite_retries(self, channel: str, now: float) -> None:
        """Channel works again: make every pending retry on it due immediately."""
        col = {"email": "email_retry_at", "feishu": "feishu_retry_at"}[channel]
        self.execute(f"UPDATE alerts SET {col} = ? WHERE {col} > ?", (now, now))

    def delete_alerts(self, ids: list[int] | None) -> int:
        """Delete the given alerts, or all alerts when ids is None."""
        if ids is None:
            return self.execute("DELETE FROM alerts").rowcount
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        return self.execute(f"DELETE FROM alerts WHERE id IN ({marks})", tuple(ids)).rowcount

    def list_alerts(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.all(
            "SELECT a.id, a.model_id, a.ts, a.kind, a.subject, a.message, a.email_status, a.feishu_status, "
            "m.name AS model_name FROM alerts a LEFT JOIN models m ON m.id = a.model_id "
            "ORDER BY a.ts DESC, a.id DESC LIMIT ?",
            (limit,),
        )
