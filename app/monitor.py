import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from . import config, netcheck
from .crypto import decrypt
from .db import Database
from .feishu import FeishuNotConfigured, Notice
from .feishu import send as send_feishu
from .incidents import SLOW, Problem, describe
from .mailer import MailNotConfigured, send_mail
from .probes import TOKEN_PARAMS, ProbeResult, probe_image, probe_text
from .scheduling import IntervalPolicy, ProbeGate, next_run, resource_key

log = logging.getLogger("monitor")

ProbeFn = Callable[[dict[str, Any], str], Awaitable[ProbeResult]]
MailFn = Callable[[dict[str, str], str, str], Awaitable[None]]
FeishuFn = Callable[[dict[str, str], Notice], Awaitable[None]]
NetworkFn = Callable[[], Awaitable[tuple[bool, list[str]]]]

KIND_LABEL = {"text": "文本/推理模型", "image": "图像模型"}
TRIGGER_LABEL = {"schedule": "定时", "manual": "手动", "fanout": "联动"}
ALERT_KIND_LABEL = {"down": "连通失败", "slow": "TTFT 异常", "changed": "异常变化", "recovered": "恢复",
                    "net_down": "监控端网络中断", "net_up": "监控端网络恢复"}
CHANNELS = ("email", "feishu")
# Delay before each retry of a failed alert delivery; the last value repeats until we give up.
RETRY_BACKOFF = (30, 60, 120, 300, 600)
RETRY_GIVE_UP_SECONDS = 24 * 3600
# Merged dispatch never holds an alert back longer than this, even while new ones keep arriving.
MERGE_MAX_WAIT_SECONDS = 300
MERGED_MAX_LINES = 50
# After the first normal cycle of an incident, confirm the recovery this soon instead of a whole interval later.
RECOVERY_RECHECK_SECONDS = 300
FANOUT_COOLDOWN_SECONDS = 1800
NET_CHECK_CACHE_SECONDS = 30
NETWORK_CODES = {"connect", "timeout", "disconnected", "http502", "http503", "http504"}
NET_KINDS = {"net_down", "net_up"}
MONITOR_SELF = "监控端"
# Each probe may make two requests (token-parameter / image-format fallback), each with its own timeout.
PROBE_HARD_LIMIT_SECONDS = {
    "text": 2 * config.TEXT_TIMEOUT_SECONDS + 30,
    "image": 2 * config.IMAGE_TIMEOUT_SECONDS + 30,
}


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def classify(r: ProbeResult, baseline: float | None, pct: float) -> str:
    """'ok' | 'slow' (TTFT over threshold) | 'down' (connectivity failure)."""
    if not r.success:
        return "down"
    if baseline and r.ttft_ms is not None and r.ttft_ms > baseline * (1 + pct / 100):
        return "slow"
    return "ok"


class Monitor:
    def __init__(
        self,
        db: Database,
        probe_fn: ProbeFn | None = None,
        mail_fn: MailFn | None = None,
        feishu_fn: FeishuFn | None = None,
        network_fn: NetworkFn | None = None,
        tick_seconds: float = 5,
    ):
        self.db = db
        self._probe_override = probe_fn
        self._mail = mail_fn or send_mail
        self._feishu = feishu_fn or send_feishu
        self._network_override = network_fn
        self._tick_seconds = tick_seconds
        self._running: dict[int, asyncio.Task] = {}
        self._queued: set[int] = set()
        self._loop_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._net_task: asyncio.Task | None = None
        self._fanout_at: dict[int, float] = {}
        self._send_lock = asyncio.Lock()
        self._net_lock = asyncio.Lock()
        self._net_cache: tuple[float, bool, list[str]] = (0.0, True, [])
        since = (db.get_settings().get("state_net_down_since") or "").strip()
        # When the monitor's own network is down: survives restarts, so an outage is reported once.
        self.outage_since: float | None = float(since) if since else None
        self._last_purge = 0.0
        self._limits_cache: tuple[float, dict[str, int]] = (0.0, {})
        self.gate = ProbeGate(self._current_limits)

    def _current_limits(self) -> dict[str, int]:
        at, cached = self._limits_cache
        if time.time() - at > 5 or not cached:
            s = self.db.get_settings()
            cached = {
                "text": int(s.get("max_concurrent_text") or 4),
                "image": int(s.get("max_concurrent_image") or 2),
                "host": int(s.get("max_concurrent_per_host") or 3),
            }
            self._limits_cache = (time.time(), cached)
        return cached

    # ---- lifecycle ----
    async def start(self) -> None:
        self.realign_schedule()
        s = self.db.get_settings()
        self.db.refresh_baseline_cache(None, int(s["baseline_window"]), int(s["baseline_min_samples"]))
        self._loop_task = asyncio.create_task(self._loop())

    def realign_schedule(self) -> None:
        """Put every already-checked model on its staggered slot, so a restart doesn't fire them all at once.
        Also applies changed intervals / peak hours right away."""
        now = time.time()
        policy = self.interval_policy()
        for m in self.db.list_models():
            if m["enabled"] and m["last_check_at"] is not None:
                self.db.update_model(m["id"], {"next_run_at": self.next_run_at(m, now, policy)})

    def interval_policy(self, settings: dict[str, str] | None = None) -> IntervalPolicy:
        return IntervalPolicy.from_settings(settings or self.db.get_settings(), config.APP_TZ)

    @staticmethod
    def next_run_at(m: dict[str, Any], now: float, policy: IntervalPolicy) -> float:
        fixed = m["interval_minutes"] if m.get("interval_mode") == "fixed" else None
        return next_run(m["id"], now, policy, fixed)

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, self._retry_task, self._flush_task, self._net_task,
                             *self._running.values()) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    def tick(self) -> None:
        now = time.time()
        if self.outage_since is None:
            for m in self.db.due_models(now):
                self.trigger(m["id"], "schedule")
        elif self._net_task is None or self._net_task.done():
            # Our own network is down: model checks would only fail, so just watch for it to come back.
            self._net_task = asyncio.create_task(self.network_ok())
        settings = self.db.get_settings()
        if (self._flush_task is None or self._flush_task.done()) and self._flush_due(now, settings):
            self._flush_task = asyncio.create_task(self.flush_alerts())
        if (self._retry_task is None or self._retry_task.done()) and self.db.due_alert_retries(now):
            self._retry_task = asyncio.create_task(self.retry_due_alerts())
        if now - self._last_purge > 3600:
            self._last_purge = now
            days = int(settings.get("retention_days") or 90)
            self.db.purge_older_than(now - days * 86400)

    def is_running(self, model_id: int) -> bool:
        return model_id in self._running

    def is_queued(self, model_id: int) -> bool:
        return model_id in self._queued

    def trigger(self, model_id: int, trigger: str) -> bool:
        if model_id in self._running:
            return False
        m = self.db.get_model(model_id)
        if not m:
            return False
        if trigger == "schedule":
            self.db.update_model(model_id, {"next_run_at": self.next_run_at(m, time.time(), self.interval_policy())})
        task = asyncio.create_task(self.run_cycle(model_id, trigger))
        self._running[model_id] = task
        task.add_done_callback(lambda _t, mid=model_id: self._running.pop(mid, None))
        return True

    # ---- core ----
    def baseline(self, m: dict[str, Any], settings: dict[str, str]) -> float | None:
        if m.get("baseline_manual_ms"):
            return float(m["baseline_manual_ms"])
        value, _ = self.db.auto_baseline(m, int(settings["baseline_window"]), int(settings["baseline_min_samples"]))
        return value

    @staticmethod
    def threshold_pct(m: dict[str, Any], settings: dict[str, str]) -> float:
        v = m.get("ttft_threshold_pct")
        return float(v) if v is not None else float(settings["ttft_threshold_pct"])

    async def _guarded_probe(self, m: dict[str, Any], key: str) -> ProbeResult:
        # Safety net on top of the per-request timeouts: whatever hangs (connection close, a library bug),
        # a probe never holds its concurrency slot longer than this.
        limit = PROBE_HARD_LIMIT_SECONDS[m["kind"]]
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(limit):
                return await self._probe(m, key)
        except TimeoutError:
            return ProbeResult(False, None, None, round((time.perf_counter() - t0) * 1000, 1), f"检测超时（>{limit}s，已强制结束）")

    async def _probe(self, m: dict[str, Any], key: str) -> ProbeResult:
        if self._probe_override:
            return await self._probe_override(m, key)
        # A fresh client per probe: a pool shared across probes can get wedged after a network outage
        # (requests left waiting for a connection that never comes) and then time out forever.
        # Pooled connections idle out after 5s anyway, so checks minutes apart never reused them.
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0), follow_redirects=False) as client:
            if m["kind"] == "image":
                return await probe_image(client, m["endpoint"], m["deployment"], key)
            r = await probe_text(client, m["endpoint"], m["deployment"], key, m["token_param"])
            if r.rejected_param:
                # Older/third-party models only accept max_tokens, newer reasoning models only max_completion_tokens.
                alt = next(p for p in TOKEN_PARAMS if p != r.rejected_param)
                r2 = await probe_text(client, m["endpoint"], m["deployment"], key, alt)
                if not r2.rejected_param:
                    m["token_param"] = alt
                    self.db.update_model(m["id"], {"token_param": alt})
                return r2
            return r

    async def run_cycle(self, model_id: int, trigger: str) -> None:
        try:
            await self._run_cycle(model_id, trigger)
        except sqlite3.IntegrityError:
            log.info("model %s deleted during check", model_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("check cycle failed for model %s", model_id)

    async def _run_cycle(self, model_id: int, trigger: str) -> None:
        m = self.db.get_model(model_id)
        if not m:
            return
        settings = self.db.get_settings()
        retries = int(settings["retry_count"])
        gap = float(settings["retry_gap_seconds"])
        pct = self.threshold_pct(m, settings)
        baseline = self.baseline(m, settings)
        try:
            key = decrypt(m["api_key_enc"])
        except RuntimeError as e:
            key, key_error = "", str(e)
        else:
            key_error = None

        cycle_id = uuid.uuid4().hex[:12]
        host = resource_key(m["endpoint"])
        attempts: list[tuple[ProbeResult, str, float, bool]] = []
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(gap)
            # A TTFT anomaly is re-measured with nothing else in flight, so a new alert rests on clean samples.
            # Not done when the model is already known to be slow (that would stall all checks every cycle),
            # nor for connectivity retests (a dead endpoint can take minutes to time out).
            exclusive = bool(attempts) and attempts[-1][1] == "slow" and m["incident_kind"] != "slow"
            self._queued.add(model_id)
            try:
                async with self.gate.slot(m["kind"], host, exclusive) as slot:
                    self._queued.discard(model_id)
                    ts = time.time()
                    if key_error:
                        r = ProbeResult(False, error=key_error)
                    else:
                        r = await self._guarded_probe(m, key)
            finally:
                self._queued.discard(model_id)
            verdict = classify(r, baseline, pct)
            # A network-type failure may be our own network rather than the model: check before blaming the model.
            net_down = verdict == "down" and _network_class(r) and not await self.network_ok(since=ts)
            self.db.insert_check(
                {
                    "model_id": model_id,
                    "cycle_id": cycle_id,
                    "attempt": attempt,
                    "trigger": trigger,
                    "ts": ts,
                    "success": int(r.success),
                    "http_status": r.http_status,
                    "ttft_ms": r.ttft_ms,
                    "total_ms": r.total_ms,
                    "anomaly": int(verdict != "ok"),
                    "baseline_ms": baseline,
                    "error": f"监控端网络异常，本次不计入：{r.error}" if net_down else r.error,
                    "concurrent": slot.concurrent,
                    "interfered": int(slot.interfered),
                    "exclusive": int(exclusive),
                    "net_down": int(net_down),
                }
            )
            if net_down:
                return  # the model's status, incident and stats stay as they were
            attempts.append((r, verdict, ts, exclusive))
            if verdict == "ok":
                break

        final = attempts[-1][1]
        self.db.refresh_baseline_cache([model_id], int(settings["baseline_window"]), int(settings["baseline_min_samples"]))
        self._handle_outcome(m, final, attempts, baseline, pct, trigger, settings)

    # ---- monitor-side network ----
    async def network_ok(self, since: float | None = None) -> bool:
        """Can the monitor reach the internet at all? A result is reused only if that check started after
        `since` (default: within the last 30 s), so a failure is never explained away by an "online" result
        from before it happened. Concurrent callers share one check.
        Entering and leaving the outage state (and its two alerts) happens here."""
        since = time.time() - NET_CHECK_CACHE_SECONDS if since is None else since
        if self._net_cache[0] > since:
            return self._net_cache[1]
        async with self._net_lock:
            started, ok, details = self._net_cache
            if started <= since:
                started = time.time()
                if self._network_override:
                    ok, details = await self._network_override()
                else:
                    try:
                        urls = netcheck.parse_urls(self.db.get_settings().get("network_check_urls", ""))
                    except ValueError:
                        urls = netcheck.parse_urls(netcheck.DEFAULT_URLS)
                    ok, details = await netcheck.check(urls, first_ok=True)
                self._net_cache = (started, ok, details)
            if not ok and self.outage_since is None:
                self._enter_outage(details)
            elif ok and self.outage_since is not None:
                self._exit_outage()
            return ok

    def _enter_outage(self, details: list[str]) -> None:
        now = time.time()
        self.outage_since = now
        self.db.set_settings({"state_net_down_since": str(now), "state_net_down_after_check": str(self.db.max_check_id())})
        log.warning("monitor network is down: %s", "; ".join(details))
        title = "[模型监控][监控端异常] 监控服务器网络中断"
        summary = ("监控服务器无法访问外部网络：所有参考地址都不可访问。\n"
                   "这是监控端自身的网络问题，不是模型故障。期间暂停模型检测和报警，检测结果不计入可用率；网络恢复后自动继续。")
        notice = Notice("down", title, summary, [("开始时间", fmt_ts(now))], details, f"时区：{config.APP_TZ.key}")
        self.db.insert_alert(None, "net_down", title, render_text(notice), notice.to_json(), model_name=MONITOR_SELF,
                             brief="监控端网络中断 · 参考地址均不可访问", queued=True)

    def _exit_outage(self) -> None:
        since, now = self.outage_since or time.time(), time.time()
        self.outage_since = None
        after = self.db.get_settings().get("state_net_down_after_check") or "0"
        self.db.set_settings({"state_net_down_since": "", "state_net_down_after_check": ""})
        skipped = self.db.count_net_down_checks(int(after) if after.isdigit() else 0)
        duration = fmt_duration(now - since)
        log.warning("monitor network is back after %s", duration)
        title = "[模型监控][监控端恢复] 监控服务器网络已恢复"
        summary = (f"监控服务器网络已恢复，中断 {duration}（{fmt_ts(since)} ~ {fmt_ts(now)[11:]}），"
                   f"期间有 {skipped} 次检测因断网未计入。各模型会在一个检测间隔内陆续恢复检测。")
        facts = [("中断时长", duration), ("未计入的检测", f"{skipped} 次")]
        notice = Notice("recovered", title, summary, facts, [], f"时区：{config.APP_TZ.key}")
        self.db.insert_alert(None, "net_up", title, render_text(notice), notice.to_json(), model_name=MONITOR_SELF,
                             brief=f"监控端网络恢复 · 中断 {duration}", queued=True)
        self.realign_schedule()  # resume on each model's own slot instead of all at once

    # ---- incidents ----
    def _handle_outcome(self, m, final, attempts, baseline, pct, trigger, settings) -> None:
        """Track the model's incident and queue at most one alert for this cycle.

        One alert when a problem starts, one when its error content changes to something not yet reported
        in this incident, and one when recovery is confirmed by `recovery_confirm_cycles` normal cycles.
        Repeats of an already reported problem are only counted.
        """
        now = time.time()
        upd: dict[str, Any] = {"status": final, "last_check_at": now}
        event, problem = None, None
        if final in ("down", "slow"):
            try:
                problem = SLOW if final == "slow" else describe(attempts[-1][0])
            except Exception:
                # Classification must never cost an alert.
                log.exception("failed to classify probe error")
                problem = Problem("other", "检测失败", "", "other")
            seen = json.loads(m["incident_sigs"] or "[]")
            if not m["incident_kind"]:
                event, seen = "open", [problem.signature]
                upd.update(incident_since=now, incident_cycles=1)
            else:
                upd["incident_cycles"] = (m["incident_cycles"] or 0) + 1
                legacy = f"legacy:{final}"
                if problem.signature not in seen and legacy in seen:
                    # Alarmed before this version: adopt the first concrete error of that kind silently.
                    seen = [s for s in seen if s != legacy] + [problem.signature]
                elif problem.signature not in seen:
                    event = "changed"
                    seen.append(problem.signature)
            upd.update(incident_kind=final, incident_sig=problem.signature, incident_sigs=json.dumps(seen[-20:]),
                       incident_label=problem.label, incident_ok=0)
        elif m["incident_kind"]:
            ok_count = (m["incident_ok"] or 0) + 1
            if ok_count >= max(1, int(settings.get("recovery_confirm_cycles") or 2)):
                event = "recovered"
                upd.update(incident_kind=None, incident_sig=None, incident_sigs=None, incident_label=None,
                           incident_since=None, incident_ok=0, incident_cycles=0)
            else:
                # One good cycle could be a fluke while the model flaps: confirm soon instead of announcing it.
                upd["incident_ok"] = ok_count
                upd["next_run_at"] = min(m["next_run_at"] or now + RECOVERY_RECHECK_SECONDS, now + RECOVERY_RECHECK_SECONDS)
        self.db.update_model(m["id"], upd)
        if event:
            self._queue_alert(m, event, final, problem, attempts, baseline, pct, trigger, settings, now)
        if event == "open" and final == "down" and problem.resource_wide:
            self._fan_out(m, settings, now)

    def _fan_out(self, m, settings, now: float) -> None:
        """A resource-wide error (bad key, network, Azure outage) likely hits every deployment of the resource:
        check the others right away, so their alerts land in the same merged message instead of trickling in
        over a whole interval."""
        if str(settings.get("alert_fanout", "1")) != "1":
            return
        rid = m["resource_id"]
        if now - self._fanout_at.get(rid, 0) < FANOUT_COOLDOWN_SECONDS:
            return
        self._fanout_at[rid] = now
        for other in self.db.models_of_resource(rid):
            if other["id"] != m["id"] and other["enabled"] and (other["last_check_at"] or 0) < now - 120:
                self.trigger(other["id"], "fanout")

    def _queue_alert(self, m, event, final, problem, attempts, baseline, pct, trigger, settings, now) -> None:
        notice, brief = self._compose(m, event, final, problem, attempts, baseline, pct, trigger, settings, now)
        kind = "changed" if event == "changed" else ("recovered" if event == "recovered" else final)
        self.db.insert_alert(m["id"], kind, notice.title, render_text(notice), notice.to_json(),
                             model_name=m["name"], resource_name=m.get("resource_name"), brief=brief, queued=True)

    # ---- dispatch ----
    def _flush_due(self, now: float, settings: dict[str, str]) -> bool:
        rows = self.db.queued_alerts()
        if not rows:
            return False
        merge = int(settings.get("alert_merge_seconds") or 0)
        newest, oldest = max(r["ts"] for r in rows), min(r["ts"] for r in rows)
        return now - newest >= merge or now - oldest >= max(MERGE_MAX_WAIT_SECONDS, merge)

    async def flush_alerts(self) -> None:
        """Send every queued alert as one message per channel (with any still-undelivered older alerts first)."""
        try:
            async with self._send_lock:
                rows = self.db.queued_alerts()
                if not rows:
                    return
                ids = {r["id"] for r in rows}
                self.db.execute(f"UPDATE alerts SET queued = 0 WHERE id IN ({', '.join('?' for _ in ids)})", tuple(ids))
                sends = []
                for ch in CHANNELS:
                    earlier = [a for a in self.db.retry_pending(ch) if a["id"] not in ids]
                    sends.append(self._send(ch, earlier + rows, {a["id"] for a in earlier}))
                await asyncio.gather(*sends)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert flush failed")

    async def retry_due_alerts(self) -> None:
        try:
            async with self._send_lock:
                now = time.time()
                for ch in CHANNELS:
                    pending = self.db.retry_pending(ch)
                    if any(a[f"{ch}_retry_at"] <= now for a in pending):
                        await self._send(ch, pending, {a["id"] for a in pending})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert retry failed")

    async def _send(self, channel: str, rows: list[dict], resend_ids: set[int]) -> None:
        if not rows:
            return
        settings = self.db.get_settings()
        notice = self._batch_notice(rows, resend_ids)
        body = render_text(notice)
        if channel == "email":
            outcome, err = await _attempt(channel, lambda: self._mail(settings, notice.title, body), MailNotConfigured)
        else:
            outcome, err = await _attempt(channel, lambda: self._feishu(settings, notice), FeishuNotConfigured)
        now = time.time()
        merged = f"（合并 {len(rows)} 条）" if len(rows) > 1 else ""
        for a in rows:
            tries = (a.get(f"{channel}_tries") or 0) + 1
            fields: dict[str, Any] = {f"{channel}_tries": tries}
            if outcome == "sent":
                fields[f"{channel}_status"] = ("已补发" if a["id"] in resend_ids else "已发送") + merged
                fields[f"{channel}_retry_at"] = None
            elif outcome == "not_configured":
                fields[f"{channel}_status"] = f"未发送：{err}"
                fields[f"{channel}_retry_at"] = None
            else:
                delay = RETRY_BACKOFF[min(tries - 1, len(RETRY_BACKOFF) - 1)]
                if now + delay - a["ts"] > RETRY_GIVE_UP_SECONDS:
                    fields[f"{channel}_status"] = f"发送失败（已重试 {tries} 次，放弃）：{err}"
                    fields[f"{channel}_retry_at"] = None
                else:
                    fields[f"{channel}_status"] = f"发送失败，将自动重试（已尝试 {tries} 次）：{err}"
                    fields[f"{channel}_retry_at"] = now + delay
            self.db.update_alert(a["id"], fields)

    @staticmethod
    def _batch_notice(rows: list[dict], resend_ids: set[int]) -> Notice:
        rows = sorted(rows, key=lambda a: (a["ts"], a["id"]))
        if len(rows) == 1:
            a = rows[0]
            n = Notice.from_json(a["notice_json"]) if a.get("notice_json") else Notice("down", a["subject"], a["message"])
            if a["id"] in resend_ids:
                n = replace(n, title=f"[补发] {n.title}",
                            summary=f"{n.summary}\n（原报警时间 {fmt_ts(a['ts'])}，当时发送失败，现补发）")
            return n
        kinds = [Notice.from_json(a["notice_json"]).kind if a.get("notice_json") else a["kind"] for a in rows]
        color = "down" if "down" in kinds else "slow" if "slow" in kinds else "recovered"
        counts = {k: sum(1 for a in rows if a["kind"] == k) for k in ALERT_KIND_LABEL}
        parts = "，".join(f"{ALERT_KIND_LABEL[k]} {n}" for k, n in counts.items() if n)
        title = f"[模型监控] {len(rows)} 条报警合并：{parts}" + ("（含补发）" if resend_ids else "")
        by_res: dict[str, list[dict]] = {}
        for a in rows:
            by_res.setdefault(MONITOR_SELF if a["kind"] in NET_KINDS else a.get("resource_name") or "-", []).append(a)
        summary_lines = []
        for res, items in by_res.items():
            if all(a["kind"] in NET_KINDS for a in items):
                summary_lines.append("监控端：" + "；".join(a.get("brief") or a["subject"] for a in items))
                continue
            c = "，".join(f"{ALERT_KIND_LABEL[k]} {n} 个" for k in ALERT_KIND_LABEL
                         if (n := sum(1 for a in items if a["kind"] == k)))
            reasons = {(a.get("brief") or "").split(" · ", 1)[-1] for a in items if a["kind"] == "down"}
            if len(reasons) == 1 and sum(1 for a in items if a["kind"] == "down") > 1:
                c += f"（均为 {reasons.pop()}）"
            summary_lines.append(f"资源 {res}：{c}")
        details = []
        for a in rows[:MERGED_MAX_LINES]:
            line = f"[{fmt_ts(a['ts'])[11:]}] {a.get('model_name') or '-'}：{a.get('brief') or a['subject']}"
            details.append(line[:120] + ("（补发）" if a["id"] in resend_ids else ""))
        if len(rows) > MERGED_MAX_LINES:
            details.append(f"…另有 {len(rows) - MERGED_MAX_LINES} 条，见平台“报警记录”")
        facts = [("报警条数", str(len(rows))), ("时间范围", f"{fmt_ts(rows[0]['ts'])} ~ {fmt_ts(rows[-1]['ts'])[11:]}")]
        footer = f"同一时间段内的报警已合并为一条消息，单条明细见平台“报警记录”。时区：{config.APP_TZ.key}"
        return Notice(color, title, "\n".join(summary_lines), facts, details, footer)

    @staticmethod
    def _compose(m, event, final, problem, attempts, baseline, pct, trigger, settings, now) -> tuple[Notice, str]:
        name = m["name"]
        retries = len(attempts) - 1
        last = attempts[-1][0]
        th = baseline * (1 + pct / 100) if baseline else None
        state_label = "连通性失败" if final == "down" else "TTFT 异常"
        if event == "recovered":
            need = max(1, int(settings.get("recovery_confirm_cycles") or 2))
            duration = fmt_duration(now - (m["incident_since"] or now))
            title = f"[模型监控][恢复] {name} 已恢复正常"
            summary = (f"已恢复正常（连续 {need} 轮检测正常）。异常持续 {duration}，期间 {m['incident_cycles'] or 1} 轮检测异常"
                       f"（{m['incident_label'] or '-'}）。")
            brief, kind = f"已恢复 · 异常持续 {duration}", "recovered"
        else:
            reason = problem.label
            if event == "changed":
                title = f"[模型监控][异常变化] {name} {state_label}（{reason}）"
                summary = (f"异常内容发生变化（本次异常开始于 {fmt_ts(m['incident_since'] or now)}，"
                           f"之前为：{m['incident_label'] or '-'}）。现为 {state_label}：{reason}。")
            elif final == "down":
                title = f"[模型监控][报警] {name} {state_label}（{reason}）"
                summary = f"{state_label}：{reason}。首次检测及 {retries} 次重测均失败。"
            else:
                title = f"[模型监控][报警] {name} {state_label}"
                summary = f"{state_label}：首次检测及 {retries} 次重测均超出预警值。"
            if problem.detail:
                summary += f"\n错误信息：{problem.detail}"
            summary += "\n同一异常不会重复报警，恢复时会再通知。"
            if final == "down":
                brief = ("异常变化 → " if event == "changed" else "") + f"连通失败 · {reason}"
            else:
                ttft = f"{last.ttft_ms:.0f} ms" if last.ttft_ms is not None else "-"
                brief = ("异常变化 → " if event == "changed" else "") + f"TTFT 异常 · 最新 {ttft}" + (f"（预警 > {th:.0f} ms）" if th else "")
            kind = final
        host = urlsplit(m["endpoint"] if "://" in m["endpoint"] else "https://" + m["endpoint"]).hostname
        facts = [
            ("模型名称", name),
            ("模型类型", KIND_LABEL.get(m["kind"], m["kind"])),
            ("部署名称", m["deployment"]),
            ("资源", m.get("resource_name") or "-"),
            ("触发方式", f"{TRIGGER_LABEL.get(trigger, trigger)}检测"),
            ("端点主机", host or "-"),
        ]
        if baseline:
            facts.append(("TTFT 基线", f"{baseline:.0f} ms，预警 +{pct:g}%（> {th:.0f} ms）"))
        else:
            facts.append(("TTFT 基线", "样本不足，尚未启用 TTFT 预警"))
        details = []
        for i, (r, v, ts, exclusive) in enumerate(attempts):
            label = "首次检测" if i == 0 else f"第 {i} 次重测" + ("（独占）" if exclusive else "")
            state = {"ok": "正常", "slow": "TTFT 超标", "down": "失败"}[v]
            ttft = f"{r.ttft_ms:.0f} ms" if r.ttft_ms is not None else "-"
            http = r.http_status if r.http_status is not None else "-"
            line = f"[{fmt_ts(ts)}] {label}：{state}，TTFT={ttft}，HTTP={http}"
            if r.error:
                line += f"，错误：{r.error}"
            details.append(line)
        return Notice(kind, title, summary, facts, details, f"时区：{config.APP_TZ.key}"), brief


def _network_class(r: ProbeResult) -> bool:
    """Failures that might be the monitor's own network rather than the model."""
    try:
        return describe(r).code in NETWORK_CODES
    except Exception:
        return False


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    if s < 86400:
        return f"{s // 3600} 小时 {s % 3600 // 60} 分钟"
    return f"{s // 86400} 天 {s % 86400 // 3600} 小时"


def render_text(n: Notice) -> str:
    lines = [n.summary, ""]
    lines += [f"{k}：{v}" for k, v in n.facts]
    if n.details:
        lines += ["", "检测明细："] + [f"  {d}" for d in n.details]
    if n.footer:
        lines += ["", n.footer]
    return "\n".join(lines)


async def _attempt(
    channel: str, send: Callable[[], Awaitable[None]], not_configured: type[Exception]
) -> tuple[str, str]:
    """Returns ('sent' | 'not_configured' | 'failed', message)."""
    try:
        await send()
        return "sent", ""
    except not_configured as e:
        return "not_configured", str(e)
    except Exception as e:
        log.warning("send alert via %s failed: %s: %s", channel, type(e).__name__, e)
        return "failed", f"{type(e).__name__}: {e}"
