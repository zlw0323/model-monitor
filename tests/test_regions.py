import sqlite3

from app import importer
from app.db import Database
from tests.test_api import login, make_client, make_resource, model_body


def test_region_and_model_default_to_resource_name_and_deployment(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c, name="美东2")
        assert res["region"] == ""
        m = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": res["id"], "deployment": "gpt-6.1-sol"}).json()
        assert m["region"] == "美东2" and m["model"] == "gpt-6.1-sol" and m["base_model"] is None

        # Two resources in one region: the region field puts them in the same column.
        r = c.put(f"/api/resources/{res['id']}", json={"name": "res-a", "endpoint": "https://res.openai.azure.com/",
                                                         "region": "  美东2 "})
        assert r.status_code == 200 and r.json()["region"] == "美东2"
        dash = c.get("/api/dashboard").json()
        assert dash["models"][0]["region"] == "美东2" and dash["models"][0]["resource_name"] == "res-a"
        assert dash["resources"] == [{"id": res["id"], "name": "res-a", "region": "美东2", "model_count": 1}]
        assert c.post("/api/resources", json={"name": "x", "endpoint": "https://x.openai.azure.com/", "api_key": "k" * 12,
                                              "region": "r" * 51}).status_code == 422

        # A differently named deployment can be grouped under the real model name.
        body = model_body(m, deployment="sol-prod", base_model=" gpt-6.1-sol ")
        m2 = c.put(f"/api/models/{m['id']}", json=body).json()
        assert m2["model"] == "gpt-6.1-sol" and m2["base_model"] == "gpt-6.1-sol" and m2["deployment"] == "sol-prod"
        m3 = c.put(f"/api/models/{m['id']}", json={**body, "base_model": "  "}).json()
        assert m3["base_model"] is None and m3["model"] == "sol-prod"
        assert c.put(f"/api/models/{m['id']}", json={**body, "base_model": "x" * 201}).status_code == 422


def test_import_model_column(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        make_resource(c, name="美东2")
        text = "资源\t类型\t部署名称\t模型\n美东2\t文本\tsol-prod\tgpt-6.1-sol\n美东2\t文本\tgpt-6-luna\t\n"
        r = c.post("/api/models/import", json={"text": text, "dry_run": False}).json()
        assert r["created"] == 2
        models = {m["deployment"]: m for m in c.get("/api/models").json()}
        assert models["sol-prod"]["model"] == "gpt-6.1-sol" and models["gpt-6-luna"]["model"] == "gpt-6-luna"


def test_importer_model_header_does_not_steal_the_name_column():
    rows = importer.parse("资源,模型名称,类型,部署名称,模型\nr,显示名,文本,dep,gpt-5\n", {"r": 1}, set(), 30)
    assert rows[0].name == "显示名" and rows[0].base_model == "gpt-5" and rows[0].status == "ok"
    tpl = importer.parse(importer.TEMPLATE.replace("myres", "r"), {"r": 1}, set(), 30)
    assert [r.status for r in tpl] == ["ok", "ok"] and tpl[1].threshold == 100 and tpl[0].base_model == ""


def test_adds_region_and_base_model_columns_to_existing_databases(tmp_path):
    path = tmp_path / "t.db"
    db = Database(path)
    rid = db.insert_resource({"name": "瑞典中", "endpoint": "https://s.openai.azure.com/", "api_key_enc": "x"})
    db.insert_model({"name": "m", "kind": "text", "resource_id": rid, "deployment": "gpt-6-luna"})
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE resources DROP COLUMN region")
    conn.execute("ALTER TABLE models DROP COLUMN base_model")
    conn.commit()
    conn.close()

    db = Database(path)
    m = db.list_models()[0]
    assert m["region"] == "瑞典中" and m["base_model"] is None
    assert db.list_resources()[0]["region"] == ""
    db.close()
