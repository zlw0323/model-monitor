import asyncio
import time

from app.crypto import encrypt
from app.db import Database
from app.mailer import MailNotConfigured
from app.monitor import Monitor
from app.probes import ProbeResult


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
    assert m["next_run_at"] > time.time() + 29 * 60
    assert h.db.due_models(time.time()) == []
