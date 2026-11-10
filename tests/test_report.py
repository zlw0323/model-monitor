import asyncio
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import feishu, report
from app.crypto import encrypt
from app.db import Database
from app.monitor import Monitor, render_text
from tests.test_api import login, make_client
from tests.test_monitor import Harness, fail

HK = ZoneInfo("Asia/Hong_Kong")
DAY = date(2026, 11, 9)


def at(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=HK).timestamp()


class Data:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "r.db")
        self.db.set_settings({"peak_schedule_enabled": "1", "peak_start": "08:00", "peak_end": "20:00"})
        self.n = 0
        us = self.db.insert_resource({"name": "us2-a", "endpoint": "https://a.openai.azure.com/", "api_key_enc": encrypt("k"),
                                      "region": "美东2"})
        se = self.db.insert_resource({"name": "se", "endpoint": "https://b.openai.azure.com/", "api_key_enc": encrypt("k"),
                                      "region": "瑞典中"})
        self.res = {"us": us, "se": se}

    def model(self, res, deployment, kind="text"):
        return self.db.insert_model({"name": f"{deployment}-{res}", "kind": kind, "resource_id": self.res[res],
                                     "deployment": deployment})

    def check(self, mid, ts, ttft=None, success=1, attempt=0, cycle=None, interfered=0, net_down=0, http=200, error=None):
        self.n += 1
        self.db.insert_check({"model_id": mid, "cycle_id": cycle or f"c{self.n}", "attempt": attempt, "trigger": "schedule",
                              "ts": ts, "success": success, "http_status": http, "ttft_ms": ttft, "total_ms": ttft,
                              "anomaly": 0, "error": error, "interfered": interfered, "net_down": net_down})

    def steady(self, mid, base_ttft, today_ttft, hours=(2, 6, 9, 12, 15, 21)):
        for d in range(1, 8):
            for h in (3, 10, 16):
                self.check(mid, at(DAY - timedelta(days=d), h), base_ttft)
        for h in hours:
            self.check(mid, at(DAY, h), today_ttft)

    def alert(self, mid, kind, ts, brief=""):
        aid = self.db.insert_alert(mid, kind, kind, kind, brief=brief)
        self.db.execute("UPDATE alerts SET ts = ? WHERE id = ?", (ts, aid))

    def build(self, **settings):
        self.db.set_settings(settings)
        notice, brief = report.build(self.db, DAY, self.db.get_settings(), HK, at(DAY, 9) + 86400)
        return notice, brief, {t: lines for t, lines in notice.sections}, render_text(notice)


def section(sections, prefix):
    return next(lines for t, lines in sections.items() if t.startswith(prefix))


def test_full_report(tmp_path):
    d = Data(tmp_path)
    a_us, a_se = d.model("us", "gpt-a"), d.model("se", "gpt-a")
    img, flaky = d.model("us", "img", kind="image"), d.model("us", "gpt-d")
    d.steady(a_us, 500, 800)  # slower +60%
    d.steady(a_se, 400, 300)  # faster -25%
    d.steady(img, 30000, 30000)
    d.check(img, at(DAY, 13), 999_000, interfered=1)  # overlapped an image probe: ignored
    d.check(img, at(DAY, 14), 999_000, net_down=1, success=0)  # monitor offline: ignored
    for h in (1, 4):
        d.check(flaky, at(DAY, h), 700)
    for h in (10, 10.5):
        cid = f"fail{h}"
        for attempt in range(4):
            d.check(flaky, at(DAY, int(h), int(h % 1 * 60)), success=0, attempt=attempt, cycle=cid, http=429,
                    error='HTTP 429: {"error":{"code":"429","message":"high demand"}}')
    d.alert(flaky, "down", at(DAY, 10), "连通失败 · 限流 / 容量不足（HTTP 429）")
    d.alert(flaky, "recovered", at(DAY, 10, 40))
    d.alert(None, "net_down", at(DAY, 22))
    d.alert(None, "net_up", at(DAY, 22, 15))

    notice, brief, sections, text = d.build()
    assert notice.kind == "report" and notice.title.startswith("[模型监控日报] 2026-11-09（周")
    assert "4 个模型" in notice.summary and "新异常 1 · 已恢复 1" in notice.summary
    assert "监控端网络中断 1 次，共 15 分钟" in notice.summary

    slower = section(sections, "TTFT 变慢")
    assert len(slower) == 1 and slower[0].startswith("gpt-a · 美东2  800 ms ↑60%")
    faster = section(sections, "TTFT 变快")
    assert faster[0].startswith("gpt-a · 瑞典中  300 ms ↓25%")
    assert "另有 1 个模型样本不足" in faster[-1]  # gpt-d had only 2 clean samples
    assert "999" not in text and "img" not in "\n".join(slower + faster)

    avail = section(sections, "可用性问题")
    assert len(avail) == 1 and avail[0].startswith("gpt-d · 美东2  50.0%  失败 2/4 轮  主要：限流 / 容量不足（HTTP 429）")
    assert avail[0].endswith("异常 40 分钟")
    assert section(sections, "异常事件") == ["gpt-d · 美东2  限流 / 容量不足（HTTP 429）  10:00 – 10:40（40 分钟）"]

    regions = section(sections, "地区对比")
    assert regions[0].startswith("美东2  可用率 88.2%") and "图像 30.0 s" in regions[0]
    assert regions[1].startswith("瑞典中  可用率 100%  文本 300 ms ↓25%")
    assert "  gpt-a：瑞典中 最快 300 ms，美东2 最慢 800 ms" in regions
    peak = section(sections, "高峰 vs 低谷")
    assert peak[0].startswith("文本：高峰") and peak[1].startswith("图像：高峰 30.0 s / 低谷 30.0 s")
    assert brief == "11-09 日报 · 可用率 91.3% · 新异常 1 · TTFT 变慢 1 · 变快 1"  # 21 of 23 rounds


def test_calm_and_empty_days(tmp_path):
    d = Data(tmp_path)
    mid = d.model("us", "gpt-a")
    notice, brief, sections, _ = d.build()
    assert "当天没有检测数据" in notice.summary and brief == "11-09 日报 · 无检测数据"

    d.steady(mid, 500, 520)
    notice, _, sections, _ = d.build(peak_schedule_enabled="0")
    assert "全天正常，TTFT 无明显波动" in notice.summary
    assert section(sections, "TTFT 波动") == ["无明显波动（所有模型中位数变化都在 ±20% 以内）"]
    assert section(sections, "可用性问题") == ["全天所有模型都连通正常"]
    assert not any(t.startswith("高峰") or t.startswith("异常事件") for t in sections)

    _, _, sections, _ = d.build(daily_report_ttft_pct="3")  # +4% now counts
    assert section(sections, "TTFT 变慢")[0].startswith("gpt-a · 美东2  520 ms ↑4%")


def test_long_lists_are_capped(tmp_path):
    d = Data(tmp_path)
    for i in range(15):
        d.steady(d.model("us", f"m{i:02d}"), 400, 400 * (1.3 + i / 10))
    _, _, sections, _ = d.build(daily_report_top_n="5")
    slower = section(sections, "TTFT 变慢")
    assert len(slower) == 6 and slower[0].startswith("m14") and slower[-1] == "…另有 10 个，见平台"


def test_ongoing_incident_and_open_before_the_day(tmp_path):
    d = Data(tmp_path)
    mid = d.model("us", "gpt-a")
    d.steady(mid, 500, 500)
    d.alert(mid, "slow", at(DAY - timedelta(days=1), 23))
    d.db.update_model(mid, {"incident_kind": "slow", "incident_label": "TTFT 超出预警值"})
    notice, _, sections, _ = d.build()
    assert section(sections, "异常事件") == ["gpt-a · 美东2  TTFT 异常  11-08 23:00 起，至 24:00 仍未恢复"]
    assert "当前仍未恢复 1（gpt-a · 美东2）" in notice.summary and "新异常 0" in notice.summary


def test_incident_cleared_by_an_edit_does_not_swallow_the_next_one(tmp_path):
    d = Data(tmp_path)
    mid = d.model("us", "gpt-a")
    d.steady(mid, 500, 500)
    d.alert(mid, "down", at(DAY - timedelta(days=2), 10), "连通失败 · 部署不存在（HTTP 404）")
    d.db.execute("INSERT INTO incident_resets (model_id, ts) VALUES (?, ?)", (mid, at(DAY - timedelta(days=2), 11)))
    d.alert(mid, "down", at(DAY, 10), "连通失败 · 限流 / 容量不足（HTTP 429）")
    d.alert(mid, "recovered", at(DAY, 11))
    notice, _, sections, _ = d.build()
    assert "新异常 1 · 已恢复 1" in notice.summary
    assert section(sections, "异常事件") == ["gpt-a · 美东2  限流 / 容量不足（HTTP 429）  10:00 – 11:00（1 小时 0 分钟）"]


def test_lost_recovery_alert_is_reported_as_unknown_end(tmp_path):
    d = Data(tmp_path)
    mid = d.model("us", "gpt-a")
    d.steady(mid, 500, 500)
    d.alert(mid, "down", at(DAY - timedelta(days=3), 10), "连通失败 · 请求超时")  # its recovered alert was deleted
    d.alert(mid, "down", at(DAY, 9), "连通失败 · 请求超时")  # ... so it closes the old one
    d.alert(mid, "recovered", at(DAY, 9, 30))
    d.alert(mid, "slow", at(DAY, 12))  # recovered alert deleted, model no longer in an incident
    notice, _, sections, _ = d.build()
    assert section(sections, "异常事件") == [
        "gpt-a · 美东2  请求超时  09:00 – 09:30（30 分钟）",
        "gpt-a · 美东2  TTFT 异常  12:00 起，结束时间未知（报警记录可能已被删除）",
    ]
    assert "新异常 2 · 已恢复 1" in notice.summary


def test_editing_the_target_records_an_incident_reset(tmp_path):
    from tests.test_api import make_resource, model_body

    with make_client(tmp_path) as c:
        login(c)
        res = make_resource(c)
        m = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": res["id"], "deployment": "a"}).json()
        db = Database(tmp_path / "api.db")
        db.update_model(m["id"], {"incident_kind": "down", "incident_label": "x"})
        c.put(f"/api/models/{m['id']}", json=model_body(m, deployment="b"))
        assert len(db.all("SELECT * FROM incident_resets WHERE model_id = ?", (m["id"],))) == 1
        c.put(f"/api/models/{m['id']}", json=model_body(m, deployment="c"))  # no open incident: nothing recorded
        assert len(db.all("SELECT * FROM incident_resets")) == 1
        db.close()


def test_report_reads_through_a_separate_read_only_connection(tmp_path):
    d = Data(tmp_path)
    conn = d.db.read_connection()
    try:
        conn.execute("SELECT COUNT(*) FROM checks").fetchone()
        try:
            conn.execute("DELETE FROM checks")
        except Exception as e:
            assert "readonly" in str(e).lower()
        else:
            raise AssertionError("read connection must be read-only")
    finally:
        conn.close()


def test_feishu_card_with_sections_fits_the_size_limit():
    big = [("段落", [f"第 {i} 行 " + "x" * 200 for i in range(300)])]
    n = feishu.Notice("report", "t", "s", footer="f", sections=big)
    payload = feishu.build_payload(n, "")
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= feishu.MAX_BODY_BYTES
    assert "见平台“报警记录”" in json.dumps(payload, ensure_ascii=False)
    assert feishu.Notice.from_json(n.to_json()) == n


def test_report_due_rules(tmp_path):
    h = Harness(tmp_path)
    s = {**h.db.get_settings(), "daily_report_time": "09:00"}
    today = date(2026, 11, 10)
    assert h.monitor.report_due(s, at(today, 8, 59)) is None
    assert h.monitor.report_due(s, at(today, 9)) == today - timedelta(days=1)
    assert h.monitor.report_due(s, at(today, 23, 59)) == today - timedelta(days=1)  # caught up later that day
    assert h.monitor.report_due({**s, "state_daily_report_day": "2026-11-09"}, at(today, 10)) is None
    assert h.monitor.report_due({**s, "daily_report_enabled": "0"}, at(today, 10)) is None


def test_scheduled_report_is_sent_once_and_never_merged(tmp_path):
    h = Harness(tmp_path)
    h.db.set_settings({"daily_report_time": "00:00"})  # due at any time of day
    mon = Monitor(h.db, mail_fn=h._mail, feishu_fn=h._feishu, network_fn=h._network)

    async def go():
        mon.tick()
        await mon._report_task
        first = mon._report_task
        mon.tick()
        assert mon._report_task is first  # already done for yesterday: no second report

    asyncio.run(go())
    rows = [a for a in h.db.list_alerts() if a["kind"] == "daily"]
    assert len(rows) == 1 and rows[0]["feishu_status"] == "已发送" and rows[0]["model_name"] == "每日情报"
    assert len(h.notices) == 1 and h.notices[0].kind == "report" and "当天没有检测数据" in h.notices[0].summary
    yesterday = (datetime.now(HK).date() - timedelta(days=1)).isoformat()
    assert h.db.get_settings()["state_daily_report_day"] == yesterday

    # A failed report delivery is retried on its own, never folded into a merged alert message.
    h.db.execute("UPDATE alerts SET feishu_retry_at = ? WHERE id = ?", (time.time() - 1, rows[0]["id"]))
    h.cycle(fail(), fail(), fail(), fail())
    assert len(h.notices) == 2 and h.notices[1].kind == "down" and "合并" not in h.notices[1].title
    asyncio.run(h.monitor.retry_due_alerts())
    assert len(h.notices) == 3 and h.notices[2].title.startswith("[补发] [模型监控日报]")


def test_scheduled_report_skipped_without_models(tmp_path):
    db = Database(tmp_path / "e.db")
    sent = []

    async def feishu_fn(settings, notice):
        sent.append(notice)

    mon = Monitor(db, feishu_fn=feishu_fn)
    assert asyncio.run(mon.send_daily_report(scheduled=True)) is None
    assert sent == [] and db.list_alerts() == [] and db.get_settings()["state_daily_report_day"]


def test_report_api(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/api/reports/daily").status_code == 401
        login(c)
        r = c.get("/api/reports/daily")
        yesterday = datetime.now(HK).date() - timedelta(days=1)
        assert r.status_code == 200 and r.json()["title"].startswith(f"[模型监控日报] {yesterday:%Y-%m-%d}")
        assert "当天没有检测数据" in r.json()["text"]
        for bad in ("2026-13-01", "abc", (datetime.now(HK).date() + timedelta(days=1)).isoformat(), "2020-01-01"):
            assert c.get(f"/api/reports/daily?day={bad}").status_code == 422, bad
        r = c.post("/api/reports/daily/send", json={"day": None})
        assert r.status_code == 200 and r.json()["feishu_status"] == "已发送"
        assert [a["kind"] for a in c.get("/api/alerts").json()] == ["daily"]

        s = c.get("/api/settings").json()
        assert (s["daily_report_enabled"], s["daily_report_time"], s["daily_report_ttft_pct"], s["daily_report_top_n"]) \
            == ("1", "09:00", "20", "10")
        assert not any(k.startswith("state_") for k in s)
        for bad in ({"daily_report_time": "25:00"}, {"daily_report_ttft_pct": "1"}, {"daily_report_top_n": "0"},
                    {"daily_report_enabled": "2"}, {"state_daily_report_day": "2026-01-01"}):
            assert c.put("/api/settings", json={"values": bad}).status_code == 422, bad
        assert c.put("/api/settings", json={"values": {"daily_report_time": "07:30"}}).json()["daily_report_time"] == "07:30"
