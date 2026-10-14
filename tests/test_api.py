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

    app = create_app(
        db_path=tmp_path / "api.db",
        monitor_factory=lambda db: Monitor(db, probe_fn=probe, mail_fn=mail, tick_seconds=0.05),
    )
    return TestClient(app)


def test_auth_required_and_login(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/api/models").status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
        assert c.get("/api/models").json() == []


def test_model_crud_and_key_not_exposed(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
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
        assert c.delete(f"/api/models/{m['id']}").status_code == 200
        assert c.get("/api/models").json() == []


def test_settings_validation_and_password_hidden(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert c.put("/api/settings", json={"values": {"retry_count": "abc"}}).status_code == 422
        assert c.put("/api/settings", json={"values": {"evil": 1}}).status_code == 422
        r = c.put("/api/settings", json={"values": {"ttft_threshold_pct": "80", "smtp_host": "smtp.x"}, "smtp_password": "p@ss"})
        s = r.json()
        assert s["ttft_threshold_pct"] == "80" and s["smtp_password_set"] is True
        assert "p@ss" not in r.text and "smtp_password_enc" not in s
        assert c.post("/api/settings/test-email").status_code == 400  # no recipients


def test_login_throttle(tmp_path):
    with make_client(tmp_path) as c:
        for _ in range(5):
            c.post("/api/login", json={"username": "admin", "password": "x"})
        assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 429
