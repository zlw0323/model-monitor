import sqlite3
import time

from app.crypto import encrypt
from app.db import Database, split_endpoint

OLD_SCHEMA = """
CREATE TABLE models (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    endpoint TEXT NOT NULL, deployment TEXT NOT NULL, api_key_enc TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 30, ttft_threshold_pct REAL, baseline_manual_ms REAL, baseline_reset_at REAL,
    token_param TEXT NOT NULL DEFAULT 'max_completion_tokens', status TEXT NOT NULL DEFAULT 'unknown',
    last_check_at REAL, next_run_at REAL, created_at REAL NOT NULL
);
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    cycle_id TEXT NOT NULL, attempt INTEGER NOT NULL, trigger TEXT NOT NULL, ts REAL NOT NULL, success INTEGER NOT NULL,
    http_status INTEGER, ttft_ms REAL, total_ms REAL, anomaly INTEGER NOT NULL DEFAULT 0, baseline_ms REAL, error TEXT
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER REFERENCES models(id) ON DELETE CASCADE, ts REAL NOT NULL,
    kind TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL, email_status TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_split_endpoint():
    assert split_endpoint("https://res.services.ai.azure.com/api/projects/p") == ("https://res.services.ai.azure.com/", None)
    full = "https://res.openai.azure.com/openai/deployments/x/chat/completions?api-version=1"
    assert split_endpoint(full) == ("https://res.openai.azure.com/", full)
    assert split_endpoint("http://127.0.0.1:9100/v1") == ("http://127.0.0.1:9100/v1", None)
    assert split_endpoint("http://127.0.0.1:9100/v1/images/generations") == (
        "http://127.0.0.1:9100/v1", "http://127.0.0.1:9100/v1/images/generations")


def test_migrates_models_into_shared_resources(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    now = time.time()
    k1, k2 = encrypt("key-one"), encrypt("key-two")
    models = [
        # Same resource (two host aliases) and same key -> one resource.
        ("a", "text", "https://myres.openai.azure.com/", "gpt-5", k1),
        ("b", "image", "https://myres.services.ai.azure.com/api/projects/p", "gpt-image-1", encrypt("key-one")),
        # Same resource but a different key -> its own resource.
        ("c", "text", "https://myres.openai.azure.com/", "o4-mini", k2),
        # Full request URL -> kept as a per-model override.
        ("d", "text", "https://other.openai.azure.com/openai/deployments/dd/chat/completions?api-version=1", "dd", k1),
        # Full URL that equals the standard v1 URL -> no override needed.
        ("e", "image", "https://myres.openai.azure.com/openai/v1/images/generations", "img", k1),
    ]
    for i, (name, kind, ep, dep, key) in enumerate(models, start=1):
        conn.execute(
            "INSERT INTO models (id, name, kind, endpoint, deployment, api_key_enc, status, last_check_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?)", (i, name, kind, ep, dep, key, now, now))
        conn.execute("INSERT INTO checks (model_id, cycle_id, attempt, trigger, ts, success, ttft_ms) "
                     "VALUES (?, 'c', 0, 'schedule', ?, 1, 100)", (i, now))
    conn.execute("INSERT INTO alerts (model_id, ts, kind, subject, message) VALUES (1, ?, 'down', 's', 'm')", (now,))
    conn.execute("UPDATE models SET interval_minutes = 120 WHERE name = 'b'")  # deliberately slower than default
    conn.commit()
    conn.close()

    db = Database(path)
    res = {r["name"]: r for r in db.list_resources()}
    assert sorted(res) == ["myres", "myres-2", "other"]
    assert res["myres"]["model_count"] == 3 and res["myres-2"]["model_count"] == 1
    by_name = {m["name"]: m for m in db.list_models()}
    assert by_name["a"]["interval_mode"] == "global"
    assert (by_name["b"]["interval_mode"], by_name["b"]["interval_minutes"]) == ("fixed", 120)
    assert by_name["a"]["resource_id"] == by_name["b"]["resource_id"] == by_name["e"]["resource_id"] != by_name["c"]["resource_id"]
    assert by_name["d"]["endpoint_override"] == models[3][2] and by_name["d"]["endpoint"] == models[3][2]
    assert by_name["e"]["endpoint_override"] is None
    assert by_name["a"]["endpoint"] == "https://myres.openai.azure.com/"
    assert by_name["a"]["status"] == "ok" and by_name["a"]["deployment"] == "gpt-5"
    # History kept, and foreign keys still cascade after the table rebuild.
    assert len(db.list_checks(1, 0)) == 1 and len(db.list_alerts()) == 1
    db.delete_model(1)
    assert db.list_checks(1, 0) == [] and db.list_alerts() == []
    assert (tmp_path / "old.db.pre-resources.bak").exists()
    db.close()

    # Opening again is a no-op.
    db = Database(path)
    assert len(db.list_resources()) == 3 and len(db.list_models()) == 4
    db.close()
