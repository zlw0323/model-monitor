import asyncio
import time

import httpx
import pytest

from app import netcheck
from app.monitor import Monitor
from app.probes import ProbeResult
from tests.test_api import login, make_client
from tests.test_monitor import Harness, ok


def connect_fail():
    return ProbeResult(False, None, None, 10, "ConnectError: [Errno 11001] getaddrinfo failed")


def expire_net_cache(h):
    h.monitor._net_cache = (0.0, h.monitor._net_cache[1], [])


def checks(h):
    return h.db.all("SELECT * FROM checks ORDER BY id")


def test_own_network_down_sends_one_alert_and_leaves_models_alone(tmp_path):
    h = Harness(tmp_path)
    assert h.cycle(ok(500))["status"] == "ok"

    h.net_up = False
    m = h.cycle(connect_fail())
    assert h.calls == 2  # no retests: they would only fail the same way
    assert m["status"] == "ok" and m["incident_kind"] is None
    last = checks(h)[-1]
    assert last["net_down"] == 1 and last["error"].startswith("监控端网络异常")
    alerts = h.db.list_alerts()
    assert [a["kind"] for a in alerts] == ["net_down"]
    assert alerts[0]["model_id"] is None and alerts[0]["model_name"] == "监控端"
    assert len(h.notices) == 1 and "监控端异常" in h.notices[0].title
    assert h.notices[0].details == ["ref.example：不可访问（ConnectError）"]
    assert h.monitor.outage_since and h.db.get_settings()["state_net_down_since"]

    # More failures during the outage (another model, a manual check): nothing new is reported.
    expire_net_cache(h)
    h.cycle(ProbeResult(False, 502, None, 10, "HTTP 502: Bad Gateway"))
    assert len(h.db.list_alerts()) == 1 and len(h.notices) == 1

    h.net_up = True
    expire_net_cache(h)

    async def back():
        assert await h.monitor.network_ok()
        await h.monitor.flush_alerts()

    asyncio.run(back())
    assert h.monitor.outage_since is None and h.db.get_settings()["state_net_down_since"] == ""
    assert [a["kind"] for a in h.db.list_alerts()] == ["net_up", "net_down"]
    assert len(h.notices) == 2 and "监控端恢复" in h.notices[1].title
    assert ("未计入的检测", "2 次") in h.notices[1].facts

    # The outage left no trace on the model: availability, trend and latest result come from real checks only.
    assert h.db.availability(h.model_id, 0) == (100.0, 1)
    assert [p[2] for p in h.db.sparklines(0, 48)[h.model_id]] == [0]
    assert h.db.latest_checks()[h.model_id]["net_down"] == 0
    assert h.db.get_model(h.model_id)["status"] == "ok"


def test_network_ok_means_the_model_is_blamed(tmp_path):
    h = Harness(tmp_path)
    m = h.cycle(connect_fail(), connect_fail(), connect_fail(), connect_fail())
    assert m["status"] == "down" and h.calls == 4
    assert h.net_calls == 4  # each failure is judged by a network check made after it
    assert [a["kind"] for a in h.db.list_alerts()] == ["down"]
    assert not any(c["net_down"] for c in checks(h))


def test_an_older_online_result_never_clears_a_newer_failure(tmp_path):
    h = Harness(tmp_path)
    h.net_up = False
    h.cycle(connect_fail())
    h.net_up = True
    expire_net_cache(h)
    asyncio.run(h.monitor.network_ok())  # outage over: "online" is now cached
    assert h.monitor.outage_since is None

    h.net_up = False  # ...and the network drops again within the cache window
    m = h.cycle(connect_fail())
    assert m["status"] == "unknown" and m["incident_kind"] is None
    assert [a["kind"] for a in h.db.list_alerts()] == ["net_down", "net_up", "net_down"]
    assert all(c["net_down"] for c in checks(h))


def test_concurrent_failures_share_one_network_check(tmp_path):
    h = Harness(tmp_path)
    others = [h.db.insert_model({"name": f"m{i}", "kind": "text", "resource_id": h.resource_id, "deployment": f"d{i}",
                                 "interval_minutes": 30}) for i in range(5)]
    gate = asyncio.Event()

    async def probe(m, key):
        await gate.wait()
        return connect_fail()

    async def slow_network():
        h.net_calls += 1
        await asyncio.sleep(0.05)
        return False, ["ref.example：不可访问（ConnectError）"]

    h.monitor._probe_override = probe
    h.monitor._network_override = slow_network
    h.db.set_settings({"max_concurrent_text": "10", "max_concurrent_per_host": "10"})

    async def go():
        tasks = [asyncio.create_task(h.monitor.run_cycle(i, "schedule")) for i in [h.model_id, *others]]
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(go())
    assert h.net_calls == 1 and len(checks(h)) == 6 and all(c["net_down"] for c in checks(h))
    assert [a["kind"] for a in h.db.list_alerts()] == ["net_down"]


def test_non_network_errors_never_check_the_network(tmp_path):
    h = Harness(tmp_path)
    h.net_up = False
    bad_key = ProbeResult(False, 401, None, 10, 'HTTP 401: {"error":{"code":"401","message":"Access denied"}}')
    m = h.cycle(bad_key, bad_key, bad_key, bad_key)
    assert m["status"] == "down" and h.net_calls == 0 and h.monitor.outage_since is None
    h.cycle(ok(400))
    assert h.net_calls == 0


def test_tick_pauses_scheduled_checks_and_resumes_on_slots(tmp_path):
    h = Harness(tmp_path)
    h.db.update_model(h.model_id, {"next_run_at": 0, "last_check_at": time.time() - 3600})
    h.monitor.outage_since = time.time() - 120
    h.db.set_settings({"state_net_down_since": str(h.monitor.outage_since)})
    h.net_up = False

    async def go():
        h.monitor.tick()
        await h.monitor._net_task
        assert h.calls == 0 and h.monitor.outage_since  # still down: no model checks
        h.net_up = True
        expire_net_cache(h)
        h.monitor.tick()
        await h.monitor._net_task
        assert h.monitor.outage_since is None
        assert h.calls == 0 and h.db.get_model(h.model_id)["next_run_at"] > time.time()  # back on its own slot
        await h.monitor.flush_alerts()

    asyncio.run(go())
    assert [a["kind"] for a in h.db.list_alerts()] == ["net_up"]  # the down alert was sent before the restart


def test_outage_state_survives_restart(tmp_path):
    h = Harness(tmp_path)
    h.db.set_settings({"state_net_down_since": "1700000000.5"})
    assert Monitor(h.db).outage_since == 1700000000.5
    h.db.set_settings({"state_net_down_since": ""})
    assert Monitor(h.db).outage_since is None


def test_short_outage_is_one_merged_message(tmp_path):
    h = Harness(tmp_path)
    h.db.set_settings({"alert_merge_seconds": "600"})
    h.net_up = False
    h.cycle(connect_fail(), flush=False)
    h.net_up = True
    expire_net_cache(h)

    async def go():
        await h.monitor.network_ok()
        await h.monitor.flush_alerts()

    asyncio.run(go())
    assert len(h.notices) == 1
    n = h.notices[0]
    assert "2 条报警合并" in n.title and "监控端网络中断 1" in n.title and "监控端网络恢复 1" in n.title
    assert n.summary.startswith("监控端：监控端网络中断")


def test_parse_urls():
    assert netcheck.parse_urls(" https://a.example/x ,\nhttp://b.example ") == ["https://a.example/x", "http://b.example"]
    assert netcheck.parse_urls(netcheck.DEFAULT_URLS)
    for bad in ("", " , ", "ftp://a.example", "https://", "a.example", "https://a.example:99999/",
                ",".join(["https://a.example"] * 6)):
        with pytest.raises(ValueError):
            netcheck.parse_urls(bad)


def test_check_any_response_counts_as_online():
    def handler(request):
        if request.url.host == "down.example":
            raise httpx.ConnectError("boom")
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    ok_, details = asyncio.run(netcheck.check(["https://down.example/", "https://up.example:8443/"], transport))
    assert ok_ and details == ["down.example：不可访问（ConnectError）", "up.example:8443：可访问（HTTP 503）"]
    ok_, details = asyncio.run(netcheck.check(["https://down.example/"], transport))
    assert not ok_


def test_check_first_ok_does_not_wait_for_a_blocked_reference():
    async def handler(request):
        if request.url.host == "blocked.example":
            await asyncio.sleep(30)
        return httpx.Response(200)

    t0 = time.perf_counter()
    ok_, details = asyncio.run(netcheck.check(["https://blocked.example/", "https://up.example/"],
                                              httpx.MockTransport(handler), first_ok=True))
    assert ok_ and details == ["up.example：可访问（HTTP 200）"] and time.perf_counter() - t0 < 2


def test_settings_api(tmp_path, monkeypatch):
    async def fake_check(urls, transport=None):
        return False, [f"{u}：不可访问" for u in urls]

    monkeypatch.setattr(netcheck, "check", fake_check)
    with make_client(tmp_path) as c:
        login(c)
        s = c.get("/api/settings").json()
        assert s["network_check_urls"] == netcheck.DEFAULT_URLS
        assert not any(k.startswith("state_") for k in s)
        assert c.put("/api/settings", json={"values": {"state_net_down_since": "1"}}).status_code == 422
        r = c.put("/api/settings", json={"values": {"network_check_urls": "ftp://x.example"}})
        assert r.status_code == 422 and "格式不正确" in r.json()["detail"]
        r = c.put("/api/settings", json={"values": {"network_check_urls": "https://a.example\nhttps://b.example"}})
        assert r.status_code == 200 and r.json()["network_check_urls"] == "https://a.example,https://b.example"

        assert c.post("/api/settings/test-network", json={"urls": "nope"}).status_code == 422
        r = c.post("/api/settings/test-network", json={})
        assert r.json() == {"ok": False, "details": ["https://a.example：不可访问", "https://b.example：不可访问"]}
        assert c.get("/api/dashboard").json()["monitor"] == {"network_down_since": None}
