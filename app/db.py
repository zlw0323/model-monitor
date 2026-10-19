import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import DEFAULT_SETTINGS

MODEL_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    deployment TEXT NOT NULL,
    endpoint_override TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    ttft_threshold_pct REAL,
    baseline_manual_ms REAL,
    baseline_reset_at REAL,
    baseline_auto_ms REAL,
    baseline_samples INTEGER NOT NULL DEFAULT 0,
    token_param TEXT NOT NULL DEFAULT 'max_completion_tokens',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_check_at REAL,
    next_run_at REAL,
    created_at REAL NOT NULL,
    incident_kind TEXT,
    incident_sig TEXT,
    incident_sigs TEXT,
    incident_label TEXT,
    incident_since REAL,
    incident_ok INTEGER NOT NULL DEFAULT 0,
    incident_cycles INTEGER NOT NULL DEFAULT 0
"""

# Columns added after the resources migration; ensured on every start for databases created in between.
MODEL_EXTRA_COLUMNS = (
    ("incident_kind", "TEXT"),
    ("incident_sig", "TEXT"),
    ("incident_sigs", "TEXT"),
    ("incident_label", "TEXT"),
    ("incident_since", "REAL"),
    ("incident_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("incident_cycles", "INTEGER NOT NULL DEFAULT 0"),
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS models ({MODEL_COLUMNS});
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
    exclusive INTEGER NOT NULL DEFAULT 0,
    net_down INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checks_model_ts ON checks(model_id, ts);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);
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
    feishu_retry_at REAL,
    model_name TEXT,
    resource_name TEXT,
    brief TEXT,
    queued INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_FULL_URL_SUFFIXES = ("/chat/completions", "/images/generations")


def split_endpoint(endpoint: str) -> tuple[str, str | None]:
    """Split a model endpoint into (resource base endpoint, per-model override).

    Azure hosts map to the v1 API no matter which path was pasted, so their base is just the host.
    A full request URL (…/chat/completions) can't be shared between text and image models,
    so it is kept as a per-model override and its base is the URL without the operation path.
    """
    ep = endpoint.strip()
    u = urlsplit(ep if "://" in ep else "https://" + ep)
    path = u.path.rstrip("/")
    full = path.endswith(_FULL_URL_SUFFIXES)
    if (u.hostname or "").lower().endswith(".azure.com"):
        base = urlunsplit((u.scheme, u.netloc, "/", "", ""))
    else:
        for s in _FULL_URL_SUFFIXES:
            if path.endswith(s):
                path = path[: -len(s)]
        base = urlunsplit((u.scheme, u.netloc, path or "/", "", ""))
    return base, (ep if full else None)


def _resource_name_for(base: str) -> str:
    host = (urlsplit(base).hostname or "resource").lower()
    if host.endswith(".azure.com"):
        return host.split(".", 1)[0]
    port = urlsplit(base).port
    return f"{host}:{port}" if port else host


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            model_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(models)")}
            if "resource_id" not in model_cols:
                self._migrate_to_resources()
            model_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(models)")}
            for col, ddl in MODEL_EXTRA_COLUMNS:
                if col not in model_cols:
                    self._conn.execute(f"ALTER TABLE models ADD COLUMN {col} {ddl}")
            # Models already alarmed before incident tracking existed: treat that as an open, already-notified
            # incident, so the upgrade doesn't re-send an alert for a problem the team already knows about.
            for kind in ("down", "slow"):
                self._conn.execute(
                    "UPDATE models SET incident_kind = ?, incident_sig = ?, incident_sigs = ?, incident_label = ?, "
                    "incident_since = COALESCE(last_check_at, ?), incident_cycles = 1 "
                    "WHERE status = ? AND incident_kind IS NULL",
                    (kind, f"legacy:{kind}", f'["legacy:{kind}"]', "升级前已报警", time.time(), kind),
                )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_models_resource ON models(resource_id)")
            check_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(checks)")}
            for col, ddl in (
                ("concurrent", "INTEGER"),
                ("interfered", "INTEGER NOT NULL DEFAULT 0"),
                ("exclusive", "INTEGER NOT NULL DEFAULT 0"),
                ("net_down", "INTEGER NOT NULL DEFAULT 0"),
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
                ("model_name", "TEXT"),
                ("resource_name", "TEXT"),
                ("brief", "TEXT"),
                ("queued", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {ddl}")
            for k, v in DEFAULT_SETTINGS.items():
                self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    def _migrate_to_resources(self) -> None:
        """Move endpoint + API key from each model into shared resources (one per distinct endpoint + key)."""
        from .crypto import decrypt
        from .probes import build_url

        old = [dict(r) for r in self._conn.execute("SELECT * FROM models ORDER BY id")]
        if old:
            backup = self._path.with_name(self._path.name + ".pre-resources.bak")
            if not backup.exists():
                dst = sqlite3.connect(str(backup))
                try:
                    self._conn.backup(dst)
                finally:
                    dst.close()
        groups: dict[tuple[str, str], int] = {}
        names: set[str] = set()
        rows = []
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN")
            for m in old:
                base, override = split_endpoint(m["endpoint"])
                if override and build_url(override, m["kind"]) == build_url(base, m["kind"]):
                    override = None  # the standard v1 URL: the resource endpoint already produces it
                try:
                    secret = decrypt(m["api_key_enc"])
                except RuntimeError:
                    secret = "enc:" + m["api_key_enc"]  # undecryptable: keep models apart rather than guess
                gkey = (_resource_name_for(base) if ".azure.com" in base else base, secret)
                if gkey not in groups:
                    name, n = _resource_name_for(base), 2
                    while name in names:
                        name, n = f"{_resource_name_for(base)}-{n}", n + 1
                    names.add(name)
                    cur = self._conn.execute(
                        "INSERT INTO resources (name, endpoint, api_key_enc, created_at) VALUES (?, ?, ?, ?)",
                        (name, base, m["api_key_enc"], time.time()),
                    )
                    groups[gkey] = int(cur.lastrowid)
                rows.append((m, groups[gkey], override))
            self._conn.execute(f"CREATE TABLE models_new ({MODEL_COLUMNS})")
            for m, rid, override in rows:
                self._conn.execute(
                    "INSERT INTO models_new (id, name, kind, resource_id, deployment, endpoint_override, enabled, "
                    "interval_minutes, ttft_threshold_pct, baseline_manual_ms, baseline_reset_at, token_param, status, "
                    "last_check_at, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m["id"], m["name"], m["kind"], rid, m["deployment"], override, m["enabled"],
                     m["interval_minutes"], m["ttft_threshold_pct"], m["baseline_manual_ms"], m["baseline_reset_at"],
                     m["token_param"], m["status"], m["last_check_at"], m["next_run_at"], m["created_at"]),
                )
            self._conn.execute("DROP TABLE models")
            self._conn.execute("ALTER TABLE models_new RENAME TO models")
            problems = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"迁移后外键检查失败: {len(problems)} 处")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

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

    # ---- resources ----
    def list_resources(self) -> list[dict[str, Any]]:
        return self.all(
            "SELECT r.*, COUNT(m.id) AS model_count FROM resources r LEFT JOIN models m ON m.resource_id = r.id "
            "GROUP BY r.id ORDER BY r.name"
        )

    def get_resource(self, resource_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM resources WHERE id = ?", (resource_id,))

    def insert_resource(self, fields: dict[str, Any]) -> int:
        fields = {**fields, "created_at": time.time()}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return int(self.execute(f"INSERT INTO resources ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid)

    def update_resource(self, resource_id: int, fields: dict[str, Any]) -> None:
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.execute(f"UPDATE resources SET {sets} WHERE id = ?", (*fields.values(), resource_id))

    def delete_resource(self, resource_id: int) -> None:
        self.execute("DELETE FROM resources WHERE id = ?", (resource_id,))

    # ---- models ----
    _MODEL_FULL = (
        "SELECT m.*, r.name AS resource_name, r.endpoint AS resource_endpoint, r.api_key_enc AS api_key_enc, "
        "COALESCE(m.endpoint_override, r.endpoint) AS endpoint FROM models m JOIN resources r ON r.id = m.resource_id"
    )

    def get_model(self, model_id: int) -> dict[str, Any] | None:
        """Model joined with its resource: `endpoint` is the effective endpoint, `api_key_enc` the resource key."""
        return self.one(f"{self._MODEL_FULL} WHERE m.id = ?", (model_id,))

    def list_models(self) -> list[dict[str, Any]]:
        return self.all(f"{self._MODEL_FULL} ORDER BY m.id")

    def models_of_resource(self, resource_id: int) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM models WHERE resource_id = ? ORDER BY id", (resource_id,))

    def model_keys(self) -> set[tuple[int, str, str]]:
        return {(r["resource_id"], r["deployment"].lower(), r["kind"]) for r in
                self.all("SELECT resource_id, deployment, kind FROM models")}

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

    def delete_models(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        return self.execute(f"DELETE FROM models WHERE id IN ({marks})", tuple(ids)).rowcount

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
        return self.one("SELECT * FROM checks WHERE model_id = ? AND net_down = 0 ORDER BY ts DESC, id DESC LIMIT 1",
                        (model_id,))

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

    def refresh_baseline_cache(self, model_ids: list[int] | None, window: int, min_samples: int) -> None:
        """Store each model's auto baseline on the model row, so the dashboard never recomputes it."""
        targets = self.all("SELECT id, baseline_reset_at FROM models" + ("" if model_ids is None else
                           f" WHERE id IN ({', '.join('?' for _ in model_ids)})"), tuple(model_ids or ()))
        for m in targets:
            value, n = self.auto_baseline(m, window, min_samples)
            self.execute("UPDATE models SET baseline_auto_ms = ?, baseline_samples = ? WHERE id = ?", (value, n, m["id"]))

    # ---- batch queries for the dashboard ----
    def latest_checks(self) -> dict[int, dict[str, Any]]:
        rows = self.all(
            "SELECT c.* FROM checks c JOIN ("
            " SELECT (SELECT id FROM checks WHERE model_id = m.id AND net_down = 0 ORDER BY ts DESC, id DESC LIMIT 1) AS cid"
            " FROM models m) x ON c.id = x.cid"
        )
        return {r["model_id"]: r for r in rows}

    def availability_all(self, since_short: float, since_long: float) -> dict[int, dict[str, Any]]:
        """Per model: share of check cycles that eventually succeeded, over a short and a long window."""
        rows = self.all(
            "SELECT model_id, COUNT(*) AS n_long, SUM(ok) AS up_long, SUM(t >= ?) AS n_short, "
            "SUM(CASE WHEN t >= ? THEN ok ELSE 0 END) AS up_short FROM ("
            " SELECT model_id, MAX(success) AS ok, MIN(ts) AS t FROM checks WHERE ts >= ? AND net_down = 0"
            " GROUP BY model_id, cycle_id"
            ") GROUP BY model_id",
            (since_short, since_short, since_long),
        )
        out = {}
        for r in rows:
            out[r["model_id"]] = {
                "short": (r["up_short"] / r["n_short"] * 100) if r["n_short"] else None,
                "n_short": r["n_short"] or 0,
                "long": r["up_long"] / r["n_long"] * 100 if r["n_long"] else None,
                "n_long": r["n_long"] or 0,
            }
        return out

    def sparklines(self, since: float, max_points: int) -> dict[int, list[list]]:
        """Recent first-attempt results per model as [ts, ttft_ms | None, flag] (flag 0 ok, 1 anomaly, 2 failed)."""
        rows = self.all(
            "SELECT model_id, ts, ttft_ms, success, anomaly FROM checks WHERE ts >= ? AND attempt = 0 AND net_down = 0 "
            "ORDER BY model_id, ts",
            (since,),
        )
        out: dict[int, list[list]] = {}
        for r in rows:
            flag = 2 if not r["success"] else 1 if r["anomaly"] else 0
            out.setdefault(r["model_id"], []).append(
                [int(r["ts"]), round(r["ttft_ms"]) if r["ttft_ms"] is not None else None, flag]
            )
        return {k: v[-max_points:] for k, v in out.items()}

    def availability(self, model_id: int, since: float) -> tuple[float | None, int]:
        """Share of check cycles (scheduled + manual) that eventually succeeded."""
        row = self.one(
            "SELECT COUNT(*) AS n, SUM(ok) AS up FROM ("
            " SELECT MAX(success) AS ok FROM checks WHERE model_id = ? AND ts >= ? AND net_down = 0 GROUP BY cycle_id)",
            (model_id, since),
        )
        if not row or not row["n"]:
            return None, 0
        return row["up"] / row["n"] * 100, row["n"]

    def count_net_down_checks(self, after_id: int) -> int:
        return self.one("SELECT COUNT(*) AS n FROM checks WHERE net_down = 1 AND id > ?", (after_id,))["n"]

    def max_check_id(self) -> int:
        return self.one("SELECT COALESCE(MAX(id), 0) AS n FROM checks")["n"]

    def purge_older_than(self, cutoff: float) -> None:
        self.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))

    # ---- alerts ----
    def insert_alert(self, model_id: int | None, kind: str, subject: str, message: str, notice_json: str = "",
                     model_name: str | None = None, resource_name: str | None = None, brief: str | None = None,
                     queued: bool = False) -> int:
        status = "待合并发送" if queued else "发送中"
        cur = self.execute(
            "INSERT INTO alerts (model_id, ts, kind, subject, message, email_status, feishu_status, notice_json, "
            "model_name, resource_name, brief, queued) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model_id, time.time(), kind, subject, message, status, status, notice_json or None,
             model_name, resource_name, brief, int(queued)),
        )
        return int(cur.lastrowid)

    def queued_alerts(self) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM alerts WHERE queued = 1 ORDER BY ts, id")

    def retry_pending(self, channel: str) -> list[dict[str, Any]]:
        col = {"email": "email_retry_at", "feishu": "feishu_retry_at"}[channel]
        return self.all(f"SELECT * FROM alerts WHERE {col} IS NOT NULL ORDER BY ts, id")

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
            "SELECT a.id, a.model_id, a.ts, a.kind, a.subject, a.message, a.email_status, a.feishu_status, a.brief, "
            "COALESCE(m.name, a.model_name) AS model_name, a.resource_name "
            "FROM alerts a LEFT JOIN models m ON m.id = a.model_id "
            "ORDER BY a.ts DESC, a.id DESC LIMIT ?",
            (limit,),
        )
