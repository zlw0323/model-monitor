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


def login(c):
    assert c.post("/api/login", json={"username": "admin", "password": "test-password"}).status_code == 200


def make_resource(c, name="res", endpoint="https://res.openai.azure.com/", key="secret-key-1234567890"):
    r = c.post("/api/resources", json={"name": name, "endpoint": endpoint, "api_key": key})
    assert r.status_code == 200, r.text
    return r.json()


def model_body(m, **over):
    keys = ("name", "kind", "resource_id", "deployment", "endpoint_override", "enabled", "interval_minutes",
            "ttft_threshold_pct", "baseline_manual_ms")
    return {**{k: m[k] for k in keys}, **over}


def test_model_crud_with_resource_and_key_not_exposed(tmp_path):
    import time

    with make_client(tmp_path) as c:
        login(c)
        assert c.post("/api/resources", json={"name": "x", "endpoint": "https://x.openai.azure.com/"}).status_code == 422
        res = make_resource(c)
        assert res["api_key_masked"] == "secr****7890"
        r = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": 999, "deployment": "gpt-5"})
        assert r.status_code == 422  # unknown resource
        r = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": res["id"], "deployment": "gpt-5"})
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["resource_name"] == "res" and m["request_url"] == "https://res.openai.azure.com/openai/v1/chat/completions"
        assert "secret-key" not in r.text and "api_key" not in r.text

        # Same resource + deployment + kind twice is rejected.
        dup = c.post("/api/models", json={"name": "g2", "kind": "text", "resource_id": res["id"], "deployment": "GPT-5"})
        assert dup.status_code == 409

        # Scheduler should pick up the new model almost immediately.
        for _ in range(100):
            time.sleep(0.05)
            models = c.get("/api/models").json()
            if models[0]["latest"]:
                break
        assert models[0]["latest"]["ttft_ms"] == 123.0
        assert models[0]["status"] == "ok"
        assert models[0]["availability_24h"] == 100
        assert len(c.get(f"/api/models/{m['id']}/checks").json()) == 1

        r = c.put(f"/api/models/{m['id']}", json=model_body(m, interval_minutes=15))
        assert r.status_code == 200 and r.json()["interval_minutes"] == 15
        # A per-model URL may not send the resource key to another host.
        r = c.put(f"/api/models/{m['id']}", json=model_body(m, endpoint_override="https://attacker.example.com/v1/chat/completions"))
        assert r.status_code == 422
        ok_override = "https://res.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=x"
        r = c.put(f"/api/models/{m['id']}", json=model_body(m, endpoint_override=ok_override))
        assert r.status_code == 200 and r.json()["request_url"] == ok_override
        assert c.delete(f"/api/models/{m['id']}").status_code == 200
        assert c.get("/api/models").json() == []


def test_resource_update_rules(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c)
        other = make_resource(c, name="other", endpoint="https://other.openai.azure.com/")
        assert c.post("/api/resources", json={"name": "RES", "endpoint": "https://z.openai.azure.com/", "api_key": "k" * 12}).status_code == 409
        m = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": res["id"], "deployment": "gpt-5"}).json()

        # Moving the endpoint to another host needs the key again.
        body = {"name": "res", "endpoint": "https://attacker.example.com/"}
        assert c.put(f"/api/resources/{res['id']}", json=body).status_code == 422
        # Downgrading to plain http on the same host would send the key unencrypted: also needs the key.
        assert c.put(f"/api/resources/{res['id']}", json={"name": "res", "endpoint": "http://res.openai.azure.com/"}).status_code == 422
        bad_override = model_body(m, endpoint_override="http://res.openai.azure.com/openai/v1/chat/completions")
        assert c.put(f"/api/models/{m['id']}", json=bad_override).status_code == 422
        # Same host, new key: fine, and the key stays hidden.
        r = c.put(f"/api/resources/{res['id']}", json={"name": "res2", "endpoint": "https://res.openai.azure.com/", "api_key": "new-key-abcdef123"})
        assert r.status_code == 200 and "new-key" not in r.text and r.json()["name"] == "res2"
        # Renaming to an existing name is rejected.
        assert c.put(f"/api/resources/{res['id']}", json={"name": "Other", "endpoint": "https://res.openai.azure.com/"}).status_code == 409

        # Resources with models can't be deleted; empty ones can.
        assert c.delete(f"/api/resources/{res['id']}").status_code == 409
        assert c.delete(f"/api/resources/{other['id']}").status_code == 200
        lst = c.get("/api/resources").json()
        assert [x["name"] for x in lst] == ["res2"] and lst[0]["model_count"] == 1
        assert c.get(f"/api/models").json()[0]["resource_name"] == "res2"
        assert m["id"]


def test_bulk_actions(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c)
        ids = [c.post("/api/models", json={"name": f"m{i}", "kind": "text", "resource_id": res["id"], "deployment": f"d{i}"}).json()["id"]
               for i in range(3)]
        r = c.post("/api/models/bulk", json={"ids": ids[:2], "action": "disable"}).json()
        assert r["affected"] == 2
        assert [m["enabled"] for m in c.get("/api/models").json()] == [False, False, True]
        r = c.post("/api/models/bulk", json={"ids": ids, "action": "enable"}).json()
        assert r["affected"] == 2  # the third was already enabled
        r = c.post("/api/models/bulk", json={"ids": ids, "action": "run"})
        assert r.status_code == 200, r.text
        assert r.json()["matched"] == 3
        assert c.post("/api/models/bulk", json={"ids": [], "action": "delete"}).status_code == 422
        assert c.post("/api/models/bulk", json={"ids": ids, "action": "explode"}).status_code == 422
        r = c.post("/api/models/bulk", json={"ids": ids + [999], "action": "delete"}).json()
        assert r["matched"] == 3 and r["affected"] == 3
        assert c.get("/api/models").json() == []


def test_import_preview_and_create(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c, name="MyRes")
        c.post("/api/models", json={"name": "existing", "kind": "text", "resource_id": res["id"], "deployment": "gpt-5"})
        text = (
            "资源\t名称\t类型\t部署名称\t检测间隔\t预警阈值\n"
            "myres\tGPT 5\t文本\tgpt-5\t30\t\n"            # duplicate of existing
            "myres\t图像\t图像\tgpt-image-1\t60\t100%\n"     # ok
            "myres\t\t推理模型\to4-mini\t\t\n"              # ok, name defaults to deployment
            "nores\tx\t文本\tx\t\t\n"                       # unknown resource
            "myres\ty\t视频\ty\t\t\n"                       # bad kind
            "myres\tz\t文本\tz\tabc\t\n"                    # bad interval
            "myres\tw\t文本\tw\tinf\t\n"                    # not a finite number
            "myres\to4 again\ttext\tO4-MINI\t\t\n"          # duplicate within file
        )
        r = c.post("/api/models/import", json={"text": text, "dry_run": True})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["summary"] == {"ok": 2, "duplicate": 2, "error": 4} and data["created"] == 0
        rows = {row["line"]: row for row in data["rows"]}
        assert rows[3]["interval"] == 60 and rows[3]["threshold"] == 100 and rows[3]["kind"] == "image"
        assert rows[4]["name"] == "o4-mini" and rows[4]["interval"] == 30
        assert "不存在" in rows[5]["message"]
        assert len(c.get("/api/models").json()) == 1  # dry run created nothing

        data = c.post("/api/models/import", json={"text": text, "dry_run": False}).json()
        assert data["created"] == 2
        models = c.get("/api/models").json()
        assert sorted(m["deployment"] for m in models) == ["gpt-5", "gpt-image-1", "o4-mini"]
        import time
        new = [m for m in models if m["deployment"] != "gpt-5"]
        assert all(time.time() < m["next_run_at"] <= time.time() + 310 for m in new)  # staggered, not all at once

        csv_text = "resource,name,kind,deployment\nMyRes,c1,text,c1\n"
        assert c.post("/api/models/import", json={"text": csv_text, "dry_run": False}).json()["created"] == 1
        assert c.post("/api/models/import", json={"text": "a,b\n1,2", "dry_run": True}).status_code == 422
        assert c.post("/api/models/import", json={"text": "资源,类型\nmyres,文本", "dry_run": True}).status_code == 422
        tpl = c.get("/api/models/import-template")
        assert tpl.status_code == 200 and "部署名称" in tpl.text and "attachment" in tpl.headers["content-disposition"]


def test_dashboard_batch(tmp_path):
    import time

    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c)
        for i in range(3):
            c.post("/api/models", json={"name": f"m{i}", "kind": "text", "resource_id": res["id"], "deployment": f"d{i}"})
        for _ in range(100):
            time.sleep(0.05)
            d = c.get("/api/dashboard").json()
            if all(m["latest"] for m in d["models"]):
                break
        assert len(d["models"]) == 3 and d["resources"] == [{"id": res["id"], "name": "res", "model_count": 3}]
        for m in d["models"]:
            assert m["availability_24h"] == 100 and m["latest"]["ttft_ms"] == 123.0
            assert m["spark"] and m["spark"][0][1] == 123 and m["spark"][0][2] == 0
        assert "secret-key" not in c.get("/api/dashboard").text


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
