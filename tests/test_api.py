from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.monitor import Monitor
from app.probes import ProbeResult


def make_client(tmp_path):
    async def probe(m, key):
        return ProbeResult(True, 200, 123.0, 150.0)

    async def mail(settings, subject, body):
        pass

    async def feishu(settings, notice):
        pass

    app = create_app(
        db_path=tmp_path / "api.db",
        monitor_factory=lambda db: Monitor(db, probe_fn=probe, mail_fn=mail, feishu_fn=feishu, tick_seconds=0.05),
    )
    return TestClient(app)


def test_auth_required_and_login(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/api/models").status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200
        assert c.get("/api/models").json() == []


def test_model_crud_and_key_not_exposed(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "test-password"})
        r = c.post("/api/models", json={"name": "g", "kind": "text", "endpoint": "https://r.openai.azure.com/", "deployment": "gpt-5"})
        assert r.status_code == 422  # api key required
        r = c.post(
            "/api/models",
            json={
                "name": "g",
                "kind": "text",
                "endpoint": "https://r.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=x",
                "api_key": "secret-key-1234567890",
            },
        )
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["deployment"] == "gpt-5"
        assert "secret-key-1234567890" not in r.text and m["api_key_masked"] == "secr****7890"

        # Scheduler should pick up the new model almost immediately.
        import time

        for _ in range(100):
            time.sleep(0.05)
            models = c.get("/api/models").json()
            if models[0]["latest"]:
                break
        assert models[0]["latest"]["ttft_ms"] == 123.0
        assert models[0]["status"] == "ok"
        assert models[0]["availability_24h"] == 100
        assert len(c.get(f"/api/models/{m['id']}/checks").json()) == 1

        r = c.put(f"/api/models/{m['id']}", json={**{k: m[k] for k in ("name", "kind", "endpoint", "deployment")}, "interval_minutes": 15})
        assert r.status_code == 200 and r.json()["interval_minutes"] == 15
        # Pointing the stored key at another host requires re-entering it.
        moved = {**{k: m[k] for k in ("name", "kind", "deployment")}, "endpoint": "https://attacker.example.com/v1"}
        r = c.put(f"/api/models/{m['id']}", json=moved)
        assert r.status_code == 422 and "API Key" in r.json()["detail"]
        assert c.put(f"/api/models/{m['id']}", json={**moved, "api_key": "new-key-123456"}).status_code == 200
        assert c.delete(f"/api/models/{m['id']}").status_code == 200
        assert c.get("/api/models").json() == []


def test_settings_validation_and_password_hidden(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "test-password"})
        assert c.put("/api/settings", json={"values": {"retry_count": "abc"}}).status_code == 422
        assert c.put("/api/settings", json={"values": {"evil": 1}}).status_code == 422
        r = c.put("/api/settings", json={"values": {"ttft_threshold_pct": "80", "smtp_host": "smtp.x"}, "smtp_password": "p@ss"})
        s = r.json()
        assert s["ttft_threshold_pct"] == "80" and s["smtp_password_set"] is True
        assert "p@ss" not in r.text and "smtp_password_enc" not in s
        # Changing the SMTP server while a password is stored requires re-entering it.
        r = c.put("/api/settings", json={"values": {"smtp_host": "evil.example.com", "smtp_security": "none"}})
        assert r.status_code == 422
        assert c.put("/api/settings", json={"values": {"smtp_host": "smtp.x", "retry_count": "2"}}).status_code == 200
        r = c.put("/api/settings", json={"values": {"smtp_host": "smtp.y"}, "smtp_password": "new"})
        assert r.status_code == 200
        assert c.post("/api/settings/test-email").status_code == 400  # no recipients


def test_login_throttle(tmp_path):
    with make_client(tmp_path) as c:
        for _ in range(5):
            c.post("/api/login", json={"username": "admin", "password": "x"})
        assert c.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 429


def test_assets_are_versioned_and_not_cached(tmp_path):
    with make_client(tmp_path) as c:
        r = c.get("/")
        html = r.text
        assert "/static/app.js?v=" in html and "/static/style.css?v=" in html
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
        # A form submitted without JS must not leak its fields into a URL or be processed.
        r = c.post("/api/form-fallback", data={"feishu_secret": "x"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/d1234567-0000-0000-0000-abcdefabcdef"


def test_feishu_settings_hidden_and_validated(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "test-password"})
        assert c.post("/api/settings/test-feishu").status_code == 400  # not configured
        assert c.put("/api/settings", json={"feishu_webhook": "https://evil.example.com/x"}).status_code == 422
        r = c.put("/api/settings", json={"feishu_webhook": HOOK, "feishu_secret": "my-sign-secret"})
        assert r.status_code == 200
        s = r.json()
        assert s["feishu_webhook_masked"].endswith("d123****cdef") and s["feishu_secret_set"] is True
        assert "abcdefabcdef" not in r.text and "my-sign-secret" not in r.text
        assert not any(k.endswith("_enc") for k in s)
        # Saving other settings keeps the stored webhook/secret.
        s = c.put("/api/settings", json={"values": {"retry_count": "3"}}).json()
        assert s["feishu_secret_set"] is True and s["feishu_webhook_masked"]
        s = c.put("/api/settings", json={"feishu_webhook_clear": True, "feishu_secret_clear": True}).json()
        assert s["feishu_webhook_masked"] == "" and s["feishu_secret_set"] is False


def test_delete_alerts(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "test-password"})
        db: Database = c.app.state.db
        ids = [db.insert_alert(None, "down", f"a{i}", "msg") for i in range(4)]
        assert c.post("/api/alerts/delete", json={}).status_code == 422
        assert c.post("/api/alerts/delete", json={"ids": ids[:2]}).json() == {"deleted": 2}
        assert [a["subject"] for a in c.get("/api/alerts").json()] == ["a3", "a2"]
        assert c.post("/api/alerts/delete", json={"all": True}).json() == {"deleted": 2}
        assert c.get("/api/alerts").json() == []
        c.post("/api/logout")
        assert c.post("/api/alerts/delete", json={"all": True}).status_code == 401


def test_session_invalidated_when_password_changes(tmp_path, monkeypatch):
    from app import config

    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "test-password"})
        assert c.get("/api/me").status_code == 200
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "another-password")
        assert c.get("/api/me").status_code == 401


def test_httpx_does_not_log_urls():
    import logging

    import app.main  # noqa: F401  (applies logging configuration)

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
