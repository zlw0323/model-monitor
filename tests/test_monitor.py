import asyncio
import time

from app.crypto import encrypt
from app.db import Database
from app.feishu import FeishuNotConfigured
from app.mailer import MailNotConfigured
from app.monitor import Monitor
from app.probes import ProbeResult
from app.scheduling import ProbeGate, next_slot, resource_key


def ok(ms):
    return ProbeResult(True, 200, ms, ms)


def fail(msg="HTTP 503"):
    return ProbeResult(False, 503, None, 10, msg)


class Harness:
    def __init__(self, tmp_path, results=()):
        self.db = Database(tmp_path / "t.db")
        self.db.set_settings({"retry_gap_seconds": "0", "baseline_min_samples": "3", "smtp_host": "x", "smtp_to": "a@b.c"})
        self.queue = list(results)
        self.mails = []
        self.notices = []
        self.calls = 0
        self.monitor = Monitor(self.db, probe_fn=self._probe, mail_fn=self._mail, feishu_fn=self._feishu)
        self.model_id = self.db.insert_model(
            {"name": "m1", "kind": "text", "endpoint": "https://r.openai.azure.com", "deployment": "d",
             "api_key_enc": encrypt("k"), "interval_minutes": 30}
        )

    async def _probe(self, m, key):
        assert key == "k"
        self.calls += 1
        return self.queue.pop(0)

    async def _mail(self, settings, subject, body):
        self.mails.append((subject, body))

    async def _feishu(self, settings, notice):
        self.notices.append(notice)

    def cycle(self, *results):
        self.queue.extend(results)
        asyncio.run(self.monitor.run_cycle(self.model_id, "schedule"))
        assert not self.queue, "unused probe results"
        return self.db.get_model(self.model_id)


def test_connectivity_retries_three_times_then_alerts(tmp_path):
    h = Harness(tmp_path)
    m = h.cycle(fail(), fail(), fail(), fail())
    assert h.calls == 4  # initial + 3 retests
    assert m["status"] == "down"
    assert len(h.mails) == 1 and "连通性失败" in h.mails[0][0]
    assert "第 3 次重测" in h.mails[0][1]
    alerts = h.db.list_alerts()
    assert alerts[0]["kind"] == "down" and alerts[0]["email_status"] == "已发送"
    assert alerts[0]["feishu_status"] == "已发送"
    assert len(h.notices) == 1 and h.notices[0].kind == "down" and h.notices[0].title == h.mails[0][0]
    assert len(h.notices[0].details) == 4

    # Still down next cycle: no duplicate mail.
    h.cycle(fail(), fail(), fail(), fail())
    assert len(h.mails) == 1 and len(h.notices) == 1

    # Recovery mail.
    m = h.cycle(ok(500))
    assert m["status"] == "ok" and len(h.mails) == 2 and "恢复" in h.mails[1][0]
    assert h.notices[1].kind == "recovered"


def test_transient_failure_recovers_without_alert(tmp_path):
    h = Harness(tmp_path)
    m = h.cycle(fail(), fail(), ok(400))
    assert h.calls == 3 and m["status"] == "ok" and not h.mails


def test_ttft_learning_then_anomaly_alert(tmp_path):
    h = Harness(tmp_path)
    for v in (1000, 1100, 900):
        h.cycle(ok(v))
    model = h.db.get_model(h.model_id)
    assert h.monitor.baseline(model, h.db.get_settings()) == 1000

    # 50% threshold -> limit 1500 ms. One slow then a normal retest: no alert.
    m = h.cycle(ok(2000), ok(1200))
    assert m["status"] == "ok" and not h.mails

    # Slow on initial + all 3 retests -> TTFT alert.
    m = h.cycle(ok(2000), ok(2100), ok(1900), ok(2500))
    assert m["status"] == "slow"
    assert len(h.mails) == 1 and "TTFT 异常" in h.mails[0][0]
    # Anomalous samples must not pollute the baseline.
    assert h.monitor.baseline(h.db.get_model(h.model_id), h.db.get_settings()) == 1050


def test_no_ttft_alert_while_learning(tmp_path):
    h = Harness(tmp_path)
    m = h.cycle(ok(100))
    m = h.cycle(ok(99999))
    assert m["status"] == "ok" and not h.mails


def test_manual_baseline_and_per_model_threshold(tmp_path):
    h = Harness(tmp_path)
    h.db.update_model(h.model_id, {"baseline_manual_ms": 1000, "ttft_threshold_pct": 20})
    m = h.cycle(ok(1300), ok(1250), ok(1210), ok(1201))
    assert m["status"] == "slow"


def test_reset_baseline(tmp_path):
    h = Harness(tmp_path)
    for v in (1000, 1000, 1000):
        h.cycle(ok(v))
    h.db.update_model(h.model_id, {"baseline_reset_at": time.time() + 1})
    assert h.monitor.baseline(h.db.get_model(h.model_id), h.db.get_settings()) is None


def test_availability_counts_cycles(tmp_path):
    h = Harness(tmp_path)
    h.cycle(ok(100))
    h.cycle(fail(), ok(100))
    h.cycle(fail(), fail(), fail(), fail())
    pct, n = h.db.availability(h.model_id, 0)
    assert n == 3 and round(pct, 1) == 66.7


def test_mail_not_configured_is_recorded(tmp_path):
    h = Harness(tmp_path)

    async def no_mail(settings, subject, body):
        raise MailNotConfigured("未配置 SMTP 服务器或收件人")

    async def broken_feishu(settings, notice):
        raise RuntimeError("boom")

    h.monitor._mail = no_mail
    h.monitor._feishu = broken_feishu
    h.cycle(fail(), fail(), fail(), fail())
    a = h.db.list_alerts()[0]
    assert a["email_status"].startswith("未发送")
    assert a["feishu_status"].startswith("发送失败") and "boom" in a["feishu_status"]


def test_failed_feishu_alert_is_resent_before_recovery(tmp_path):
    h = Harness(tmp_path)
    network = {"up": False}

    async def flaky_feishu(settings, notice):
        if not network["up"]:
            raise ConnectionError("All connection attempts failed")
        h.notices.append(notice)

    h.monitor._feishu = flaky_feishu
    h.cycle(fail(), fail(), fail(), fail())
    a = h.db.list_alerts()[0]
    assert "将自动重试" in a["feishu_status"] and a["email_status"] == "已发送"
    raw = h.db.get_alert(a["id"])
    assert raw["feishu_retry_at"] > time.time() + 20 and raw["email_retry_at"] is None

    # Network back: the recovery cycle first re-sends the missed "down" alert, then sends "recovered".
    network["up"] = True
    h.cycle(ok(500))
    assert [n.kind for n in h.notices] == ["down", "recovered"]
    assert h.notices[0].title.startswith("[补发]") and "原报警时间" in h.notices[0].summary
    statuses = {x["kind"]: x["feishu_status"] for x in h.db.list_alerts()}
    assert statuses == {"down": "已补发", "recovered": "已发送"}
    # Email was delivered the first time and must not be sent twice.
    assert len(h.mails) == 2


def test_background_retry_and_give_up(tmp_path):
    h = Harness(tmp_path)
    attempts = []

    async def broken_feishu(settings, notice):
        attempts.append(notice.title)
        raise ConnectionError("down")

    h.monitor._feishu = broken_feishu
    h.cycle(fail(), fail(), fail(), fail())
    alert_id = h.db.list_alerts()[0]["id"]

    # Not due yet: the worker does nothing.
    asyncio.run(h.monitor.retry_due_alerts())
    assert len(attempts) == 1

    # Make it due; still failing -> tries increments and a later retry is scheduled.
    h.db.update_alert(alert_id, {"feishu_retry_at": time.time() - 1})
    asyncio.run(h.monitor.retry_due_alerts())
    a = h.db.get_alert(alert_id)
    assert len(attempts) == 2 and attempts[1].startswith("[补发]")
    assert a["feishu_tries"] == 2 and a["feishu_retry_at"] > time.time()

    # Older than 24h -> give up.
    h.db.update_alert(alert_id, {"ts": time.time() - 25 * 3600, "feishu_retry_at": time.time() - 1})
    asyncio.run(h.monitor.retry_due_alerts())
    a = h.db.get_alert(alert_id)
    assert a["feishu_retry_at"] is None and "放弃" in a["feishu_status"]


def test_not_configured_is_not_retried(tmp_path):
    h = Harness(tmp_path)

    async def no_feishu(settings, notice):
        raise FeishuNotConfigured("未配置飞书 Webhook")

    h.monitor._feishu = no_feishu
    h.cycle(fail(), fail(), fail(), fail())
    a = h.db.get_alert(h.db.list_alerts()[0]["id"])
    assert a["feishu_status"].startswith("未发送") and a["feishu_retry_at"] is None


def test_hung_probe_is_force_stopped_and_frees_its_slot(tmp_path, monkeypatch):
    import app.monitor as monitor_mod

    monkeypatch.setitem(monitor_mod.PROBE_HARD_LIMIT_SECONDS, "text", 0.2)
    h = Harness(tmp_path)

    async def hang(m, key):
        await asyncio.sleep(3600)

    h.monitor._probe_override = hang
    h.db.set_settings({"retry_count": "0"})
    t0 = time.monotonic()
    m = h.cycle()
    assert time.monotonic() - t0 < 5
    row = h.db.list_checks(h.model_id, 0)[0]
    assert m["status"] == "down" and "强制结束" in row["error"]
    assert h.monitor.gate.inflight == 0


def test_each_probe_uses_its_own_http_client(tmp_path, monkeypatch):
    import app.monitor as monitor_mod

    seen = []

    async def fake_probe_text(client, endpoint, deployment, key, token_param):
        seen.append(client)
        assert not client.is_closed
        return ok(100)

    monkeypatch.setattr(monitor_mod, "probe_text", fake_probe_text)
    h = Harness(tmp_path)
    h.monitor._probe_override = None
    model = h.db.get_model(h.model_id)
    asyncio.run(h.monitor._probe(model, "k"))
    asyncio.run(h.monitor._probe(model, "k"))
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert all(c.is_closed for c in seen)  # nothing outlives a probe, so a wedged pool can't persist


def test_scheduler_trigger_sets_next_run(tmp_path):
    h = Harness(tmp_path)

    async def run():
        h.queue.append(ok(100))
        h.monitor.tick()
        assert h.monitor.is_running(h.model_id)
        assert not h.monitor.trigger(h.model_id, "manual")  # already running
        while h.monitor.is_running(h.model_id):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    m = h.db.get_model(h.model_id)
    now = time.time()
    assert now < m["next_run_at"] <= now + 30 * 60
    assert m["next_run_at"] == next_slot(h.model_id, 30, m["next_run_at"] - 30 * 60)  # on the model's own slot
    assert h.db.due_models(now) == []


def test_slots_are_spread_and_stable():
    period = 30 * 60
    now = 1_800_000_000.0
    offsets = sorted((next_slot(i, 30, now) - now) for i in range(1, 101))
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    # 100 models in 30 minutes: perfectly even would be 18s apart; golden-ratio spacing stays close.
    assert max(gaps) < 3 * period / 100 and min(gaps) > 0
    # Same model always lands on the same phase of its interval.
    assert (next_slot(7, 30, now) - next_slot(7, 30, now + 12345)) % period == 0


def test_realign_on_start_spreads_checked_models(tmp_path):
    h = Harness(tmp_path)
    ids = [h.model_id] + [
        h.db.insert_model({"name": f"m{i}", "kind": "text", "endpoint": "https://r.openai.azure.com", "deployment": "d",
                           "api_key_enc": encrypt("k"), "interval_minutes": 30})
        for i in range(5)
    ]
    past = time.time() - 3600
    for i in ids[:-1]:
        h.db.update_model(i, {"next_run_at": past, "last_check_at": past})
    h.db.update_model(ids[-1], {"next_run_at": past, "last_check_at": None})  # never checked
    h.monitor.realign_schedule()
    due = {m["id"] for m in h.db.due_models(time.time())}
    assert due == {ids[-1]}  # only the never-checked model runs right away; the rest wait for their slots


def _gate(limits=None):
    lim = {"text": 4, "image": 2, "host": 3, **(limits or {})}
    return ProbeGate(lambda: lim)


def test_gate_limits_and_interference():
    gate = _gate({"image": 1})
    log = []

    async def probe(name, kind, host, hold, delay=0, exclusive=False):
        await asyncio.sleep(delay)
        async with gate.slot(kind, host, exclusive) as s:
            log.append(("start", name, time.monotonic()))
            await asyncio.sleep(hold)
        log.append(("end", name, time.monotonic()))
        return s

    async def run():
        return await asyncio.gather(
            probe("img1", "image", "a", 0.3),
            probe("img2", "image", "b", 0.1, delay=0.01),   # image limit 1 -> waits for img1
            probe("txt1", "text", "c", 0.05, delay=0.02),   # overlaps img1 -> interfered
            probe("txt2", "text", "c", 0.05, delay=0.5),    # runs alone -> clean
        )

    img1, img2, txt1, txt2 = asyncio.run(run())
    starts = {n: t for e, n, t in log if e == "start"}
    ends = {n: t for e, n, t in log if e == "end"}
    assert starts["img2"] >= ends["img1"] - 0.01
    assert txt1.interfered and txt1.concurrent >= 1
    assert not txt2.interfered and txt2.concurrent == 0
    assert img1.concurrent >= 1 and not img1.interfered  # only overlapped a text probe


def test_gate_exclusive_waits_and_blocks():
    gate = _gate()
    order = []

    async def probe(name, hold, delay=0, exclusive=False):
        await asyncio.sleep(delay)
        async with gate.slot("text", name, exclusive) as s:
            order.append(f"+{name}")
            await asyncio.sleep(hold)
            order.append(f"-{name}")
        return s

    async def run():
        return await asyncio.gather(
            probe("a", 0.2),
            probe("x", 0.1, delay=0.05, exclusive=True),  # waits for a to finish
            probe("b", 0.05, delay=0.1),                  # arrives while x is waiting -> must go after x
        )

    a, x, b = asyncio.run(run())
    assert order == ["+a", "-a", "+x", "-x", "+b", "-b"]
    assert x.concurrent == 0 and not x.interfered


def test_slow_retests_run_exclusive_and_are_recorded(tmp_path):
    h = Harness(tmp_path)
    h.db.update_model(h.model_id, {"baseline_manual_ms": 1000})
    h.cycle(ok(2000), ok(900))
    rows = sorted(h.db.list_checks(h.model_id, 0), key=lambda r: r["attempt"])
    assert [r["exclusive"] for r in rows] == [0, 1]
    assert all(r["concurrent"] == 0 for r in rows)
    # Connectivity retests are not exclusive (a dead endpoint must not stall every other check).
    h.cycle(fail(), ok(900))
    last = [r for r in h.db.list_checks(h.model_id, 0) if r["attempt"] == 1][0]
    assert last["exclusive"] == 0


def test_already_slow_model_does_not_retest_exclusively(tmp_path):
    h = Harness(tmp_path)
    h.db.update_model(h.model_id, {"baseline_manual_ms": 1000})
    m = h.cycle(ok(2000), ok(2000), ok(2000), ok(2000))
    assert m["status"] == "slow"
    h.cycle(ok(2000), ok(2000), ok(2000), ok(2000))
    latest_cycle = sorted(h.db.list_checks(h.model_id, 0), key=lambda r: r["id"])[-4:]
    assert [r["exclusive"] for r in latest_cycle] == [0, 0, 0, 0]


def test_resource_key_groups_azure_host_aliases():
    keys = {
        resource_key("https://myres.openai.azure.com/"),
        resource_key("https://MyRes.services.ai.azure.com/api/projects/p"),
        resource_key("myres.cognitiveservices.azure.com"),
    }
    assert keys == {"azure:myres"}
    assert resource_key("https://other.openai.azure.com") != "azure:myres"
    assert resource_key("http://127.0.0.1:9100/v1") == "127.0.0.1"


def test_interfered_samples_excluded_from_baseline(tmp_path):
    h = Harness(tmp_path)
    for v in (1000, 1000, 1000):
        h.cycle(ok(v))
    h.db.execute("UPDATE checks SET ttft_ms = 9000, interfered = 1 WHERE id = (SELECT MAX(id) FROM checks)")
    h.cycle(ok(1100))
    # 3 clean samples remain (1000, 1000, 1100) -> median 1000; the 9000 interfered sample is ignored.
    assert h.monitor.baseline(h.db.get_model(h.model_id), h.db.get_settings()) == 1000
