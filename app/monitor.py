import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from . import config
from .crypto import decrypt
from .db import Database
from .feishu import FeishuNotConfigured, Notice
from .feishu import send as send_feishu
from .mailer import MailNotConfigured, send_mail
from .probes import TOKEN_PARAMS, ProbeResult, probe_image, probe_text

log = logging.getLogger("monitor")

ProbeFn = Callable[[dict[str, Any], str], Awaitable[ProbeResult]]
MailFn = Callable[[dict[str, str], str, str], Awaitable[None]]
FeishuFn = Callable[[dict[str, str], Notice], Awaitable[None]]

KIND_LABEL = {"text": "文本/推理模型", "image": "图像模型"}
TRIGGER_LABEL = {"schedule": "定时", "manual": "手动"}
CHANNELS = ("email", "feishu")
# Delay before each retry of a failed alert delivery; the last value repeats until we give up.
RETRY_BACKOFF = (30, 60, 120, 300, 600)
RETRY_GIVE_UP_SECONDS = 24 * 3600


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
        tick_seconds: float = 5,
    ):
        self.db = db
        self._probe_override = probe_fn
        self._mail = mail_fn or send_mail
        self._feishu = feishu_fn or send_feishu
        self._tick_seconds = tick_seconds
        self._client: httpx.AsyncClient | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._last_purge = 0.0

    # ---- lifecycle ----
    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0), follow_redirects=False)
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, self._retry_task, *self._running.values()) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    def tick(self) -> None:
        now = time.time()
        for m in self.db.due_models(now):
            self.trigger(m["id"], "schedule")
        if (self._retry_task is None or self._retry_task.done()) and self.db.due_alert_retries(now):
            self._retry_task = asyncio.create_task(self.retry_due_alerts())
        if now - self._last_purge > 3600:
            self._last_purge = now
            days = int(self.db.get_settings().get("retention_days") or 90)
            self.db.purge_older_than(now - days * 86400)

    def is_running(self, model_id: int) -> bool:
        return model_id in self._running

    def trigger(self, model_id: int, trigger: str) -> bool:
        if model_id in self._running:
            return False
        m = self.db.get_model(model_id)
        if not m:
            return False
        if trigger == "schedule":
            self.db.update_model(model_id, {"next_run_at": time.time() + m["interval_minutes"] * 60})
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

    async def _probe(self, m: dict[str, Any], key: str) -> ProbeResult:
        if self._probe_override:
            return await self._probe_override(m, key)
        assert self._client is not None
        if m["kind"] == "image":
            return await probe_image(self._client, m["endpoint"], m["deployment"], key)
        r = await probe_text(self._client, m["endpoint"], m["deployment"], key, m["token_param"])
        if r.rejected_param:
            # Older/third-party models only accept max_tokens, newer reasoning models only max_completion_tokens.
            alt = next(p for p in TOKEN_PARAMS if p != r.rejected_param)
            r2 = await probe_text(self._client, m["endpoint"], m["deployment"], key, alt)
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
        attempts: list[tuple[ProbeResult, str, float]] = []
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(gap)
            ts = time.time()
            r = ProbeResult(False, error=key_error) if key_error else await self._probe(m, key)
            verdict = classify(r, baseline, pct)
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
                    "error": r.error,
                }
            )
            attempts.append((r, verdict, ts))
            if verdict == "ok":
                break

        final = attempts[-1][1]
        prev = m["status"]
        self.db.update_model(model_id, {"status": final, "last_check_at": time.time()})
        await self._maybe_alert(m, prev, final, attempts, baseline, pct, trigger)

    # ---- alerting ----
    async def _maybe_alert(self, m, prev, final, attempts, baseline, pct, trigger) -> None:
        if final == prev:
            return
        if final in ("down", "slow"):
            kind = final
        elif final == "ok" and prev in ("down", "slow"):
            kind = "recovered"
        else:
            return
        notice = self._compose(m, kind, attempts, baseline, pct, trigger)
        alert_id = self.db.insert_alert(m["id"], kind, notice.title, render_text(notice), notice.to_json())
        # Earlier alerts for this model that failed to send go out first, so "down" never arrives after "recovered".
        for pending in self.db.pending_alerts(m["id"], alert_id):
            channels = [ch for ch in CHANNELS if pending[f"{ch}_retry_at"] is not None]
            await self.deliver_alert(pending["id"], channels, resend=True)
        await self.deliver_alert(alert_id, list(CHANNELS), resend=False)

    async def retry_due_alerts(self) -> None:
        try:
            now = time.time()
            for a in self.db.due_alert_retries(now):
                channels = [ch for ch in CHANNELS if a[f"{ch}_retry_at"] is not None and a[f"{ch}_retry_at"] <= now]
                await self.deliver_alert(a["id"], channels, resend=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert retry failed")

    async def deliver_alert(self, alert_id: int, channels: list[str], resend: bool) -> None:
        async with self._send_lock:
            a = self.db.get_alert(alert_id)
            if a and resend:
                # Another task may have delivered it while we waited for the lock.
                channels = [ch for ch in channels if a[f"{ch}_retry_at"] is not None]
            if not a or not channels:
                return
            if not a.get("notice_json"):
                # Rows created before retry support: nothing to resend.
                self.db.update_alert(alert_id, {f"{ch}_retry_at": None for ch in channels})
                return
            notice = Notice.from_json(a["notice_json"])
            if resend:
                notice = replace(
                    notice,
                    title=f"[补发] {notice.title}",
                    summary=f"{notice.summary}\n（原报警时间 {fmt_ts(a['ts'])}，当时发送失败，现补发）",
                )
            body = render_text(notice)
            settings = self.db.get_settings()
            senders = {
                "email": lambda: self._mail(settings, notice.title, body),
                "feishu": lambda: self._feishu(settings, notice),
            }
            not_configured = {"email": MailNotConfigured, "feishu": FeishuNotConfigured}
            results = await asyncio.gather(*(_attempt(ch, senders[ch], not_configured[ch]) for ch in channels))

            now = time.time()
            fields: dict[str, Any] = {}
            for ch, (outcome, err) in zip(channels, results):
                tries = (a.get(f"{ch}_tries") or 0) + 1
                fields[f"{ch}_tries"] = tries
                if outcome == "sent":
                    fields[f"{ch}_status"] = "已补发" if resend else "已发送"
                    fields[f"{ch}_retry_at"] = None
                elif outcome == "not_configured":
                    fields[f"{ch}_status"] = f"未发送：{err}"
                    fields[f"{ch}_retry_at"] = None
                else:
                    delay = RETRY_BACKOFF[min(tries - 1, len(RETRY_BACKOFF) - 1)]
                    if now + delay - a["ts"] > RETRY_GIVE_UP_SECONDS:
                        fields[f"{ch}_status"] = f"发送失败（已重试 {tries} 次，放弃）：{err}"
                        fields[f"{ch}_retry_at"] = None
                    else:
                        fields[f"{ch}_status"] = f"发送失败，将自动重试（已尝试 {tries} 次）：{err}"
                        fields[f"{ch}_retry_at"] = now + delay
            self.db.update_alert(alert_id, fields)
            for ch, (outcome, _) in zip(channels, results):
                if outcome == "sent":
                    self.db.expedite_retries(ch, now)

    @staticmethod
    def _compose(m, kind, attempts, baseline, pct, trigger) -> Notice:
        name = m["name"]
        retries = len(attempts) - 1
        if kind == "down":
            title = f"[模型监控][报警] {name} 连通性失败"
            summary = f"连通性失败：首次检测及 {retries} 次重测均失败。"
        elif kind == "slow":
            title = f"[模型监控][报警] {name} TTFT 异常"
            summary = f"TTFT 异常：首次检测及 {retries} 次重测均超出预警值。"
        else:
            title = f"[模型监控][恢复] {name} 已恢复正常"
            summary = "模型已恢复正常。"
        host = urlsplit(m["endpoint"] if "://" in m["endpoint"] else "https://" + m["endpoint"]).hostname
        facts = [
            ("模型名称", name),
            ("模型类型", KIND_LABEL.get(m["kind"], m["kind"])),
            ("部署名称", m["deployment"]),
            ("触发方式", f"{TRIGGER_LABEL.get(trigger, trigger)}检测"),
            ("端点主机", host or "-"),
        ]
        if baseline:
            facts.append(("TTFT 基线", f"{baseline:.0f} ms，预警 +{pct:g}%（> {baseline * (1 + pct / 100):.0f} ms）"))
        else:
            facts.append(("TTFT 基线", "样本不足，尚未启用 TTFT 预警"))
        details = []
        for i, (r, v, ts) in enumerate(attempts):
            label = "首次检测" if i == 0 else f"第 {i} 次重测"
            state = {"ok": "正常", "slow": "TTFT 超标", "down": "失败"}[v]
            ttft = f"{r.ttft_ms:.0f} ms" if r.ttft_ms is not None else "-"
            http = r.http_status if r.http_status is not None else "-"
            line = f"[{fmt_ts(ts)}] {label}：{state}，TTFT={ttft}，HTTP={http}"
            if r.error:
                line += f"，错误：{r.error}"
            details.append(line)
        return Notice(kind, title, summary, facts, details, f"时区：{config.APP_TZ.key}")


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
