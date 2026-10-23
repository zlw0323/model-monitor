import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import Database
from app.scheduling import IntervalPolicy, next_run, next_slot, parse_hhmm
from tests.test_api import login, make_client, make_resource, model_body

HK = ZoneInfo("Asia/Hong_Kong")


def at(h, m=0, day=23):
    return datetime(2026, 10, day, h, m, tzinfo=HK).timestamp()


def policy(**over):
    s = {"peak_schedule_enabled": "1", "peak_start": "08:00", "peak_end": "20:00", "peak_interval_minutes": "15",
         "offpeak_interval_minutes": "60", "default_interval_minutes": "30", **over}
    return IntervalPolicy.from_settings(s, HK)


def test_peak_window_and_intervals():
    p = policy()
    assert [p.interval_at(at(h, m)) for h, m in ((7, 59), (8, 0), (19, 59), (20, 0), (0, 0))] == [60, 15, 15, 60, 60]
    assert p.max_interval() == 60
    assert p.next_change(at(7, 30)) == at(8) and p.next_change(at(8)) == at(20) and p.next_change(at(21)) == at(8, day=24)

    night = policy(peak_start="22:00", peak_end="02:00")  # window across midnight
    assert [night.is_peak(at(h)) for h in (21, 22, 23, 1, 2, 3)] == [False, True, True, True, False, False]

    off = policy(peak_schedule_enabled="0")
    assert off.interval_at(at(9)) == off.interval_at(at(23)) == 30 and off.next_change(at(9)) is None
    assert policy(peak_start="08:00", peak_end="08:00").enabled is False  # empty window: treated as off
    assert policy(peak_start="bad").peak_start == 8 * 60  # broken stored value falls back to the default


def test_next_run_switches_interval_at_window_boundaries():
    p = policy()
    for mid in range(1, 201):
        # Shortly before peak: either the off-peak slot before 08:00, or the 15-minute slot right after it.
        t = next_run(mid, at(7, 50), p)
        assert at(7, 50) < t < at(8) or at(8) < t <= at(8, 15)
        if t > at(8):
            assert t == next_slot(mid, 15, at(8))
        # Shortly before off-peak: a peak slot before 20:00, or the model's hourly slot after it.
        t = next_run(mid, at(19, 55), p)
        assert at(19, 55) < t < at(20) or at(20) < t <= at(21)
        # During peak and off-peak the normal staggered slots are used.
        assert next_run(mid, at(10), p) == next_slot(mid, 15, at(10))
        assert next_run(mid, at(23), p) == next_slot(mid, 60, at(23))
        # A fixed-interval model ignores the schedule.
        assert next_run(mid, at(7, 50), p, fixed_minutes=30) == next_slot(mid, 30, at(7, 50))

    # Entering peak, 200 models are spread over the first 15 minutes instead of all starting at 08:00.
    starts = sorted(next_run(mid, at(8) - 1, p) for mid in range(1, 201))
    after = [s for s in starts if s > at(8)]
    assert len(after) > 150 and max(after) - min(after) > 14 * 60
    assert max(b - a for a, b in zip(after, after[1:])) < 30


def test_parse_hhmm():
    assert parse_hhmm("08:00") == 480 and parse_hhmm(" 23:59 ") == 1439
    for bad in ("8:00", "24:00", "12:60", "", "ab:cd"):
        try:
            parse_hhmm(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_settings_api_validation_and_realign(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        s = c.get("/api/settings").json()
        assert (s["peak_schedule_enabled"], s["peak_start"], s["peak_end"]) == ("1", "08:00", "20:00")
        assert (s["peak_interval_minutes"], s["offpeak_interval_minutes"]) == ("15", "60")
        for bad in ({"peak_start": "25:00"}, {"peak_end": "8点"}, {"peak_start": "09:00", "peak_end": "09:00"},
                    {"peak_interval_minutes": "0"}, {"offpeak_interval_minutes": "2000"}, {"peak_schedule_enabled": "2"}):
            assert c.put("/api/settings", json={"values": bad}).status_code == 422, bad

        res = make_resource(c)
        m = c.post("/api/models", json={"name": "g", "kind": "text", "resource_id": res["id"], "deployment": "d"}).json()
        assert m["interval_mode"] == "global"
        for _ in range(100):  # the first check runs right away
            if c.get("/api/models").json()[0]["last_check_at"]:
                break
            time.sleep(0.05)

        r = c.put("/api/settings", json={"values": {"peak_schedule_enabled": "0", "default_interval_minutes": "1440"}})
        assert r.status_code == 200
        dash = c.get("/api/dashboard").json()
        assert dash["schedule"]["enabled"] is False and dash["schedule"]["interval_now"] == 1440
        assert dash["models"][0]["interval_now"] == dash["models"][0]["interval_max"] == 1440
        # Saving a shorter interval reschedules right away instead of after the next (day-long) wait.
        c.put("/api/settings", json={"values": {"default_interval_minutes": "1"}})
        assert c.get("/api/models").json()[0]["next_run_at"] <= time.time() + 61

        r = c.put("/api/settings", json={"values": {"peak_schedule_enabled": "1", "peak_start": "22:00", "peak_end": "02:00",
                                                    "peak_interval_minutes": "5", "offpeak_interval_minutes": "120"}})
        assert r.status_code == 200 and r.json()["peak_end"] == "02:00"
        sch = c.get("/api/dashboard").json()["schedule"]
        assert sch["enabled"] and sch["interval_now"] in (5, 120) and sch["next_change"] > time.time()

        fixed = c.put(f"/api/models/{m['id']}", json=model_body(m, interval_mode="fixed", interval_minutes=45)).json()
        assert fixed["interval_mode"] == "fixed" and fixed["interval_now"] == fixed["interval_max"] == 45
        assert fixed["next_run_at"] <= time.time() + 45 * 60
        assert c.put(f"/api/models/{m['id']}", json=model_body(m, interval_mode="weekly")).status_code == 422


def test_import_interval_column_sets_mode(tmp_path):
    with make_client(tmp_path) as c:
        login(c)
        make_resource(c, name="r")
        text = "资源\t类型\t部署名称\t检测间隔\nr\t文本\ta\t\nr\t文本\tb\t10\n"
        assert c.post("/api/models/import", json={"text": text, "dry_run": False}).json()["created"] == 2
        models = {m["deployment"]: m for m in c.get("/api/models").json()}
        assert models["a"]["interval_mode"] == "global"
        assert models["b"]["interval_mode"] == "fixed" and models["b"]["interval_minutes"] == 10


def test_long_intervals_never_wait_longer_than_the_longest_interval():
    # Intervals longer than their own window (e.g. twice in business hours, once at night) used to skip days.
    import random

    rnd = random.Random(7)
    for _ in range(200):
        start, length = rnd.randrange(0, 1440, 30), rnd.randrange(30, 1410, 30)
        hhmm = lambda m: f"{m // 60 % 24:02d}:{m % 60:02d}"
        p = policy(peak_start=hhmm(start), peak_end=hhmm(start + length),
                   peak_interval_minutes=str(rnd.choice([15, 60, 480, 720])),
                   offpeak_interval_minutes=str(rnd.choice([60, 720, 1440])))
        now = at(0) + rnd.randrange(0, 7 * 86400)
        for mid in range(1, 40):
            t = next_run(mid, now, p)
            assert now < t <= now + p.max_interval() * 60


def test_existing_models_follow_the_schedule_after_upgrade(tmp_path):
    path = tmp_path / "t.db"
    db = Database(path)
    rid = db.insert_resource({"name": "r", "endpoint": "https://r.openai.azure.com/", "api_key_enc": "x"})
    db.insert_model({"name": "m", "kind": "text", "resource_id": rid, "deployment": "d", "interval_minutes": 30})
    db.insert_model({"name": "slow", "kind": "image", "resource_id": rid, "deployment": "i", "interval_minutes": 120})
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE models DROP COLUMN interval_mode")
    conn.execute("DELETE FROM settings WHERE key LIKE 'peak%' OR key = 'offpeak_interval_minutes'")
    conn.commit()
    conn.close()

    db = Database(path)
    modes = {m["name"]: (m["interval_mode"], m["interval_minutes"]) for m in db.list_models()}
    # On the old default -> follows the schedule; deliberately slowed down -> keeps its own interval.
    assert modes == {"m": ("global", 30), "slow": ("fixed", 120)}
    s = db.get_settings()
    assert (s["peak_schedule_enabled"], s["peak_start"], s["peak_interval_minutes"]) == ("1", "08:00", "15")
    db.close()
    db = Database(path)  # reopening doesn't touch the modes again
    assert {m["name"]: m["interval_mode"] for m in db.list_models()} == {"m": "global", "slow": "fixed"}
    db.close()
