import asyncio
import json
import time

from app.crypto import encrypt
from app.db import Database
from app.feishu import MAX_BODY_BYTES, Notice, build_payload
from app.incidents import describe
from app.probes import ProbeResult
from tests.test_monitor import Harness, fail, ok

AZURE_429 = 'HTTP 429: {"error":{"code":"429","message":"The system is currently experiencing high demand. Retry after %d seconds."}}'


def http(status, body):
    return ProbeResult(False, status, None, 50, f"HTTP {status}: {body}")


def r429(n=12):
    return http(429, AZURE_429.split(": ", 1)[1] % n)


def test_broken_unicode_in_upstream_error_still_alerts(tmp_path):
    body = '{"error":{"code":"x","message":"boom \\ud800 \\u0000 end"}}'
    p = describe(http(500, body))
    assert p.signature.startswith("http500:") and "\x00" not in p.detail
    h = Harness(tmp_path)
    m = h.cycle(*(http(500, body),) * 4)
    assert m["status"] == "down" and len(h.notices) == 1


def test_signatures():
    a, b = describe(r429(12)), describe(r429(30))
    assert a.signature == b.signature and a.code == "http429" and "限流" in a.label
    assert "high demand" in a.detail
    assert describe(http(401, '{"error":{"code":"401","message":"Access denied due to invalid key"}}')).resource_wide
    assert not a.resource_wide
    assert describe(http(500, "boom")).signature != describe(http(500, "other failure")).signature
    assert describe(ProbeResult(False, None, None, 1, "超时（>120s）")).code == "timeout"
    assert describe(ProbeResult(False, None, None, 1, "网络错误: ConnectError: ")).signature == "connect"
    assert describe(ProbeResult(False, None, None, 1, "网络错误: ConnectError: All connection attempts failed")).signature == "connect"


def test_flapping_model_alerts_once_until_confirmed_recovery(tmp_path):
    h = Harness(tmp_path, confirm_cycles=2)
    h.db.update_model(h.model_id, {"baseline_manual_ms": 1000})
    down = (r429(), r429(), r429(), r429())
    slow = (ok(2000), ok(2000), ok(2000), ok(2000))

    h.cycle(*down)                       # incident opens -> 1 alert
    h.cycle(*down)                       # same problem -> counted only
    m = h.cycle(ok(500))                 # first good cycle: not announced, recheck soon
    assert m["incident_ok"] == 1 and m["incident_kind"] == "down"
    assert m["next_run_at"] <= time.time() + 300
    h.cycle(*down)                       # flaps back with the same error -> still one incident, no alert
    h.cycle(*slow)                       # different problem (TTFT) -> one "changed" alert
    h.cycle(*down)                       # already reported in this incident -> no alert
    h.cycle(*slow)                       # already reported -> no alert
    h.cycle(ok(500))
    m = h.cycle(ok(500))                 # 2 good cycles in a row -> recovered
    assert [n.kind for n in h.notices] == ["down", "slow", "recovered"]
    assert len(h.mails) == 3
    assert "异常变化" in h.notices[1].title
    assert "已恢复正常（连续 2 轮检测正常）" in h.notices[2].summary and "期间 6 轮检测异常" in h.notices[2].summary
    assert m["incident_kind"] is None and m["status"] == "ok"
    kinds = [a["kind"] for a in reversed(h.db.list_alerts())]
    assert kinds == ["down", "changed", "recovered"]


def test_new_error_content_alerts_again(tmp_path):
    h = Harness(tmp_path)
    h.cycle(r429(1), r429(2), r429(3), r429(4))
    h.cycle(*(http(500, "internal failure"),) * 4)       # new error content -> alert
    h.cycle(r429(9), r429(9), r429(9), r429(9))          # back to the 429 already reported -> silent
    assert [("变化" in n.title) for n in h.notices] == [False, True]
    assert "服务端内部错误" in h.notices[1].title


def test_merged_dispatch_groups_by_resource(tmp_path):
    h = Harness(tmp_path)
    other_res = h.db.insert_resource({"name": "r2", "endpoint": "https://r2.openai.azure.com/", "api_key_enc": encrypt("k")})
    m2 = h.db.insert_model({"name": "m2", "kind": "text", "resource_id": h.resource_id, "deployment": "d2"})
    m3 = h.db.insert_model({"name": "m3", "kind": "text", "resource_id": other_res, "deployment": "d3"})
    h.db.set_settings({"alert_fanout": "0"})
    nokey = http(401, '{"error":{"code":"401","message":"Access denied due to invalid key"}}')
    h.cycle(*(nokey,) * 4, flush=False)
    h.cycle(*(nokey,) * 4, model_id=m2, flush=False)
    h.cycle(*(r429(),) * 4, model_id=m3, flush=False)
    assert h.notices == [] and all(a["email_status"] == "待合并发送" for a in h.db.list_alerts())
    asyncio.run(h.monitor.flush_alerts())
    assert len(h.notices) == 1 and len(h.mails) == 1
    n = h.notices[0]
    assert n.kind == "down" and "3 条报警合并：连通失败 3" in n.title
    assert "资源 r：连通失败 2 个（均为 认证失败（API Key 无效）（HTTP 401））" in n.summary
    assert "资源 r2：连通失败 1 个" in n.summary
    assert len(n.details) == 3 and "m1" in n.details[0]
    assert {a["feishu_status"] for a in h.db.list_alerts()} == {"已发送（合并 3 条）"}


def test_flush_waits_for_quiet_window(tmp_path):
    h = Harness(tmp_path)
    h.db.set_settings({"alert_merge_seconds": "60"})
    h.cycle(fail(), fail(), fail(), fail(), flush=False)
    s = h.db.get_settings()
    now = time.time()
    assert not h.monitor._flush_due(now, s)
    assert h.monitor._flush_due(now + 61, s)
    # A steady stream of new alerts can't hold the oldest back longer than 5 minutes.
    h.db.execute("UPDATE alerts SET ts = ?", (now - 301,))
    h.db.insert_alert(h.model_id, "down", "x", "y", queued=True)
    assert h.monitor._flush_due(time.time(), s)


def _cycle_with_fanout(h, first_results, others_ok):
    async def go():
        h.queue.extend(first_results)
        h.queue.extend([ok(100)] * others_ok)
        await h.monitor.run_cycle(h.model_id, "schedule")
        while h.monitor._running:
            await asyncio.sleep(0.01)
        await h.monitor.flush_alerts()

    asyncio.run(go())


def test_fanout_checks_rest_of_resource_on_resource_wide_error(tmp_path):
    h = Harness(tmp_path)
    ids = [h.db.insert_model({"name": f"x{i}", "kind": "text", "resource_id": h.resource_id, "deployment": f"x{i}"}) for i in range(3)]
    nokey = http(401, '{"error":{"code":"401","message":"Access denied due to invalid key"}}')
    _cycle_with_fanout(h, [nokey] * 4, others_ok=3)
    for i in ids:
        rows = h.db.list_checks(i, 0)
        assert len(rows) == 1 and rows[0]["trigger"] == "fanout"
    # Cooldown: another resource-wide failure right after doesn't fan out again.
    h.db.update_model(h.model_id, {"incident_kind": None, "status": "ok"})
    _cycle_with_fanout(h, [nokey] * 4, others_ok=0)
    assert all(len(h.db.list_checks(i, 0)) == 1 for i in ids)


def test_no_fanout_for_per_deployment_errors(tmp_path):
    h = Harness(tmp_path)
    other = h.db.insert_model({"name": "x", "kind": "text", "resource_id": h.resource_id, "deployment": "x"})
    _cycle_with_fanout(h, [r429()] * 4, others_ok=0)
    assert h.db.list_checks(other, 0) == []


def test_upgrade_treats_existing_alarm_as_reported(tmp_path):
    path = tmp_path / "u.db"
    db = Database(path)
    rid = db.insert_resource({"name": "r", "endpoint": "https://r.openai.azure.com/", "api_key_enc": encrypt("k")})
    mid = db.insert_model({"name": "m", "kind": "text", "resource_id": rid, "deployment": "d", "status": "down",
                           "last_check_at": time.time()})
    db.execute("UPDATE models SET incident_kind = NULL WHERE id = ?", (mid,))
    db.close()
    m = Database(path).get_model(mid)
    assert m["incident_kind"] == "down" and json.loads(m["incident_sigs"]) == ["legacy:down"]


def test_legacy_incident_adopts_first_error_silently(tmp_path):
    h = Harness(tmp_path)
    h.db.update_model(h.model_id, {"status": "down", "incident_kind": "down", "incident_sigs": '["legacy:down"]',
                                   "incident_since": time.time() - 3600, "incident_label": "升级前已报警"})
    m = h.cycle(*(r429(),) * 4)                  # same ongoing problem after the upgrade: silent
    assert h.notices == [] and "限流" in m["incident_label"]
    h.cycle(*(http(500, "boom"),) * 4)          # a genuinely different error still alerts
    assert len(h.notices) == 1 and "异常变化" in h.notices[0].title


def test_feishu_payload_stays_under_limit_for_big_merges():
    details = [f"[12:00:{i:02d}] 很长的模型名称-{i}：连通失败 · 认证失败（API Key 无效）（HTTP 401）" * 2 for i in range(200)]
    p = build_payload(Notice("down", "t", "s", [("a", "b")], details, "f"), "")
    raw = json.dumps(p, ensure_ascii=False).encode()
    assert len(raw) <= MAX_BODY_BYTES and "行未显示" in raw.decode()
