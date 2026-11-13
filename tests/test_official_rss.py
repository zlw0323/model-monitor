import asyncio
import html
import time
from email.utils import formatdate

import httpx
import pytest

from app import official_rss
from app.db import Database
from app.feishu import FeishuError
from app.monitor import Monitor
from tests.test_api import login, make_client
from tests.test_monitor import Harness, fail


def feed(*items):
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>"
            + "".join(items) + "</channel></rss>").encode()


def item(ident="issue-1", title="Service incident", body="<p>Users cannot connect.</p>",
         status="ServiceDegradation", published=None, link="https://status.cloud.microsoft/"):
    published = time.time() if published is None else published
    return (f"<item><guid>{html.escape(ident)}</guid><title>{html.escape(title)}</title>"
            f"<description>{html.escape(body)}</description><pubDate>{formatdate(published, usegmt=True)}</pubDate>"
            f"<status>{html.escape(status)}</status><link>{html.escape(link)}</link></item>")


class Feeds:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "rss.db")
        self.responses = {k: feed() for k in official_rss.FEEDS}
        self.calls = []
        self.headers = {}
        self.status_codes = {}
        self.failures = set()
        self.watcher = official_rss.Watcher(self.db, httpx.MockTransport(self.handle))

    def handle(self, req):
        key = next(k for k, (_, u) in official_rss.FEEDS.items() if str(req.url) == u)
        self.calls.append((key, req))
        if key in self.failures:
            raise httpx.ConnectError("credential-looking upstream message should not be printed")
        return httpx.Response(self.status_codes.get(key, 200), content=self.responses[key],
                              headers=self.headers.get(key, {}))

    def poll(self):
        return asyncio.run(self.watcher.poll())

    def kinds(self):
        return [r["kind"] for r in self.db.all("SELECT kind FROM alerts ORDER BY id")]


def test_three_fixed_official_feeds_and_no_credentials(tmp_path):
    f = Feeds(tmp_path)
    f.responses["m365"] = feed(item())
    state = f.poll()
    assert len(f.calls) == len(state) == 3
    for _, req in f.calls:
        assert "authorization" not in req.headers and "api-key" not in req.headers and "cookie" not in req.headers
    assert f.kinds() == ["official_issue"]
    assert all(s["last_success"] and not s["error"] for s in state)
    row = f.db.queued_alerts()[0]
    assert row["model_id"] is None and row["model_name"] == "Microsoft 365"
    assert "官方详情：https://status.cloud.microsoft/" in row["message"]
    assert row["email_status"].startswith("不适用")


def test_fault_update_recovery_and_restart_dedup(tmp_path):
    f = Feeds(tmp_path)
    start = time.time() - 1000
    f.responses["m365"] = feed(item(published=start))
    f.poll()
    f.poll()
    # pubDate alone is not a new message.
    f.responses["m365"] = feed(item(published=start + 30))
    f.poll()
    assert f.kinds() == ["official_issue"]
    f.watcher = official_rss.Watcher(f.db, f.watcher.transport)
    f.responses["m365"] = feed(item(body="<p>Current status: Mitigation deployed.</p>", published=start + 40))
    f.poll()
    f.poll()
    assert f.kinds() == ["official_issue", "official_update"]
    # Old snapshots of this episode don't produce repeated update alerts.
    f.responses["m365"] = feed(item(published=start + 35))
    f.poll()
    assert len(f.kinds()) == 2
    f.responses["m365"] = feed(item(status="ServiceRestored", published=start + 50))
    f.poll()
    f.poll()
    assert f.kinds()[-1] == "official_recovered" and len(f.kinds()) == 3
    # Same incident ID genuinely reopens: send a new issue and a later recovery.
    f.responses["m365"] = feed(item(published=start + 60))
    f.poll()
    f.responses["m365"] = feed(item(status="ServiceRestored", published=start + 70))
    f.poll()
    assert f.kinds()[-2:] == ["official_issue", "official_recovered"]


def test_guid_replacement_correlated_by_singleton_service_and_stale_snapshots_ignored(tmp_path):
    f = Feeds(tmp_path)
    start = time.time() - 1000
    f.responses["power_platform"] = feed(item("a", published=start))
    f.poll()
    f.responses["power_platform"] = feed(item("b", body="More details", published=start + 100))
    f.poll()
    assert f.kinds() == ["official_issue", "official_update"]
    f.responses["power_platform"] = feed(item("c", status="ServiceRestored", published=start + 200))
    f.poll()
    assert f.kinds()[-1] == "official_recovered"
    assert f.db.one("SELECT COUNT(*) n FROM official_items WHERE state='active'")["n"] == 0
    f.responses["power_platform"] = feed(item("b", body="More details", published=start + 100))
    f.poll()
    f.responses["power_platform"] = feed(item("c", status="ServiceRestored", published=start + 200))
    f.poll()
    assert len(f.kinds()) == 3
    f.responses["power_platform"] = feed(item("d", published=start + 300))
    f.poll()
    assert f.kinds()[-1] == "official_issue"  # genuinely newer recurrence


@pytest.mark.parametrize("dated", [True, False])
def test_equal_or_missing_timestamp_replay_does_not_reopen_episode(tmp_path, dated):
    f = Feeds(tmp_path)
    published = time.time() - 1000
    active = item(status="ServiceDegradation", published=published)
    restored = item("other-guid", status="ServiceRestored", published=published)
    if not dated:
        import re
        active = re.sub(r"<pubDate>.*?</pubDate>", "", active)
        restored = re.sub(r"<pubDate>.*?</pubDate>", "", restored)
    for snapshot in (active, restored, active, restored):
        f.responses["m365"] = feed(snapshot)
        f.poll()
    assert f.kinds() == ["official_issue", "official_recovered"]


def test_failed_issue_is_delivered_before_recovery(tmp_path):
    h = Harness(tmp_path)
    f = Feeds(tmp_path)
    mon = h.monitor
    mon.db = f.db
    f.responses["m365"] = feed(item())
    f.poll()
    async def failing(s, n):
        raise FeishuError("mock failure")
    mon._feishu = failing
    asyncio.run(mon.flush_alerts())
    failed = f.db.retry_pending("feishu")[0]
    f.responses["m365"] = feed(item("recovery-guid", status="ServiceRestored"))
    f.poll()
    mon._feishu = h._feishu
    asyncio.run(mon.flush_alerts())
    assert h.notices == []  # respect the earlier failure's retry backoff
    assert [a["kind"] for a in f.db.queued_alerts()] == ["official_recovered"]
    f.db.update_alert(failed["id"], {"feishu_retry_at": time.time() - 1})
    asyncio.run(mon.retry_due_alerts())
    assert len(h.notices) == 2
    assert h.notices[0].kind == "down" and h.notices[1].kind == "recovered"
    assert f.db.queued_alerts() == [] and f.db.retry_pending("feishu") == []


def test_initial_history_suppressed_and_current_degradation_not_lost(tmp_path):
    f = Feeds(tmp_path)
    old = time.time() - 7 * 86400
    f.responses["azure"] = feed(item("old", status="", published=old),
                               item("closed", title="[Resolved] Old issue", status=""),
                               item("new", status=""))
    f.responses["power_platform"] = feed(item("long", status="ServiceDegradation", published=old))
    f.poll()
    assert f.kinds() == ["official_issue", "official_issue"]
    # Only a new body (not a timestamp refresh) upgrades a seeded historical item.
    f.responses["azure"] = feed(item("old", status="", published=time.time()),
                               item("closed", title="[Resolved] Old issue", status=""),
                               item("new", status=""))
    f.poll()
    assert len(f.kinds()) == 2


def test_available_placeholder_resolves_but_empty_or_failed_feed_never_does(tmp_path):
    f = Feeds(tmp_path)
    f.responses["m365"] = feed(item())
    f.poll()
    f.responses["m365"] = feed()
    f.poll()
    assert f.kinds() == ["official_issue"]
    f.failures.add("m365")
    state = f.poll()
    err = next(s for s in state if s["source"] == "m365")["error"]
    assert "ConnectError" in err and "credential" not in err
    assert f.kinds() == ["official_issue"]
    f.failures.clear()
    f.responses["m365"] = feed(item("placeholder", title="Microsoft Admin Center", status="Available"))
    f.poll()
    f.poll()
    assert f.kinds() == ["official_issue", "official_recovered"]
    f.responses["azure"] = feed(item(status=""))
    f.poll()
    f.responses["azure"] = feed()
    f.poll()
    assert f.kinds()[-1] == "official_issue"  # empty Azure is not proof of recovery


def test_etag_304_and_disable(tmp_path):
    f = Feeds(tmp_path)
    f.headers["azure"] = {"ETag": '"revision-1"', "Last-Modified": formatdate(time.time(), usegmt=True)}
    f.poll()
    f.status_codes["azure"] = 304
    f.db.set_settings({"official_rss_m365": "0", "official_rss_power_platform": "0"})
    f.calls.clear()
    state = f.poll()
    assert len(f.calls) == 1
    assert f.calls[0][1].headers["If-None-Match"] == '"revision-1"'
    assert not state[0]["error"]
    assert [s["enabled"] for s in state] == [True, False, False]


def test_parser_safety_and_azure_resolution():
    parsed = official_rss.parse_feed(feed(item(body="<script>bad()</script><p>Readable &amp; clear</p>",
                                               link="https://evil.example/phishing")), "azure")[0]
    assert "bad()" not in parsed.body and parsed.body == "Readable & clear"
    assert parsed.link == "https://azure.status.microsoft/"
    for raw in (b"<html>error</html>", b"not xml", b"<?xml version='1.0'?><!DOCTYPE rss [<!ENTITY e 'x'>]><rss/>",
                b"\xff\xfe", b"x" * (official_rss.MAX_BYTES + 1), feed(item(status="surprise")),
                feed(item(), item()), feed("<item><title>No ID</title><guid></guid><pubDate>broken</pubDate></item>")):
        with pytest.raises(official_rss.FeedError):
            official_rss.parse_feed(raw, "azure")
    r = official_rss.parse_feed(feed(item(status="", title="[Resolved] Connectivity incident")), "azure")[0]
    assert r.state == "resolved"
    r = official_rss.parse_feed(feed(item(status="", body="Current status: Mitigated.\nPrevious status: Investigating.")), "azure")[0]
    assert r.state == "resolved"
    r = official_rss.parse_feed(feed(item(status="", body="Current status: Investigating.\nPrevious issue was resolved.")), "azure")[0]
    assert r.state == "active"
    for link in ("javascript:alert(1)", "https://status.cloud.microsoft.evil.example/",
                 "https://user:password@status.cloud.microsoft/", "https://status.cloud.microsoft:99999/"):
        assert official_rss.safe_link(link, "m365") == "https://status.cloud.microsoft/"


def test_invalid_http_and_size_limits_surface_as_subscription_errors(tmp_path):
    f = Feeds(tmp_path)
    f.status_codes["azure"] = 302
    f.responses["m365"] = b"x" * (official_rss.MAX_BYTES + 1)
    f.responses["power_platform"] = b"not XML"
    state = f.poll()
    assert all(s["error"] for s in state)
    assert f.kinds() == []
    assert "HTTP 302" in state[0]["error"]


def test_dedup_and_pending_alert_saved_atomically(tmp_path, monkeypatch):
    f = Feeds(tmp_path)
    f.db.set_settings({"official_rss_m365": "0", "official_rss_power_platform": "0"})
    f.responses["azure"] = feed(item(status=""))
    original = f.db.insert_alert
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("transaction fail")
    monkeypatch.setattr(f.db, "insert_alert", broken)
    with pytest.raises(RuntimeError, match="transaction fail"):
        f.poll()
    assert f.db.queued_alerts() == [] and f.db.all("SELECT * FROM official_items") == []
    monkeypatch.setattr(f.db, "insert_alert", original)
    f.poll()
    assert f.kinds() == ["official_issue"]


def test_rss_dispatch_persistent_individual_and_retry_independent(tmp_path):
    h = Harness(tmp_path)
    f = Feeds(tmp_path)
    h.monitor.db = f.db
    f.responses["m365"] = feed(item())
    f.poll()
    asyncio.run(h.monitor.flush_alerts())
    assert len(h.notices) == 1 and not h.mails
    row = f.db.all("SELECT * FROM alerts")[0]
    assert row["feishu_status"] == "已发送" and not row["queued"]
    assert row["email_status"].startswith("不适用")
    # Durable queue from the watcher is delivered by a fresh monitor process.
    f.responses["m365"] = feed(item(body="New details"))
    f.poll()
    async def failed_send(s, n):
        raise FeishuError("mock delivery failure")
    mon = Monitor(f.db, feishu_fn=failed_send, official_feeds=False, daily_reports=False)
    asyncio.run(mon.flush_alerts())
    pending = f.db.retry_pending("feishu")
    assert len(pending) == 1 and pending[0]["kind"] == "official_update"
    mon._feishu = h._feishu
    f.db.update_alert(pending[0]["id"], {"feishu_retry_at": time.time() - 1})
    asyncio.run(mon.retry_due_alerts())
    assert len(h.notices) == 2 and h.notices[-1].title.startswith("[补发]")


def test_official_notice_does_not_short_circuit_model_alert_merge(tmp_path):
    h = Harness(tmp_path)
    h.db.set_settings({"alert_merge_seconds": "300"})
    h.cycle(fail(), fail(), fail(), fail(), flush=False)
    watcher = official_rss.Watcher(h.db)
    with h.db.transaction():
        watcher._record("m365", official_rss.parse_feed(feed(item()), "m365")[0], time.time())
    assert h.monitor._flush_due(time.time(), h.db.get_settings())
    asyncio.run(h.monitor.flush_alerts(only_due=True))
    assert len(h.notices) == 1 and "微软官方通告" in h.notices[0].title
    assert [a["kind"] for a in h.db.queued_alerts()] == ["down"]
    asyncio.run(h.monitor.flush_alerts())
    assert len(h.notices) == 2 and "连通性失败" in h.notices[-1].title


def test_rss_endpoints_and_validated_settings(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/api/settings/official-rss").status_code == 401
        assert c.post("/api/settings/official-rss/check").status_code == 401
        login(c)
        for values in ({"official_rss_azure": "2"}, {"official_rss_interval_minutes": "0"},
                       {"official_rss_interval_minutes": "61"}, {"official_rss_url": "https://evil.example"}):
            assert c.put("/api/settings", json={"values": values}).status_code == 422
        sources = c.get("/api/settings/official-rss").json()["sources"]
        assert len(sources) == 3 and all(s["enabled"] for s in sources)
        def handler(req):
            return httpx.Response(200, content=feed())
        c.app.state.monitor.rss.transport = httpx.MockTransport(handler)
        assert c.post("/api/settings/official-rss/check").status_code == 200
        assert c.get("/api/settings/official-rss").json()["sources"][0]["last_success"]
