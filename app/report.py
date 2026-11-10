"""Daily report: what happened to the monitored models on one local calendar day.

Everything comes from stored checks and alerts, so a report costs no extra model calls.
"""

import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from typing import Any

from .db import Database
from .feishu import Notice
from .incidents import describe
from .probes import ProbeResult
from .scheduling import IntervalPolicy

WEEKDAY = "一二三四五六日"
MIN_SAMPLES = 3  # per model and period, for a TTFT comparison
BASE_DAYS = 7
OPEN_KINDS = ("down", "slow")
EVENT_KINDS = ("down", "slow", "changed", "recovered", "net_down", "net_up")
KIND_NAME = {"down": "连通失败", "slow": "TTFT 异常"}


def day_bounds(day: date, tz: tzinfo) -> tuple[float, float]:
    start = datetime.combine(day, dtime(0), tzinfo=tz).timestamp()
    return start, datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=tz).timestamp()


def fmt_ttft(ms: float | None, kind: str) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f} s" if kind == "image" or ms >= 10000 else f"{ms:,.0f} ms"


def fmt_pct(change: float) -> str:
    return f"{'↑' if change >= 0 else '↓'}{abs(change):.0f}%"


def fmt_avail(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%" if v < 100 else "100%"


def fmt_minutes(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} 小时 {m % 60} 分钟" if m >= 60 else f"{max(m, 1)} 分钟"


def p90(values: list[float]) -> float:
    return statistics.quantiles(values, n=10, method="inclusive")[8] if len(values) > 1 else values[0]


def change_pct(now: float, base: float) -> float:
    return (now - base) / base * 100 if base else 0.0


def _limit(lines: list[str], top: int) -> list[str]:
    return lines[:top] + ([f"…另有 {len(lines) - top} 个，见平台"] if len(lines) > top else [])


@dataclass
class _Model:
    m: dict[str, Any]
    label: str
    today: list[tuple[float, float]] = field(default_factory=list)  # (ts, ttft) clean samples
    base: list[float] = field(default_factory=list)
    cycles: list[int] = field(default_factory=list)  # today: 1 = the cycle eventually succeeded
    failures: list[dict] = field(default_factory=list)  # today: last attempt of each failed cycle

    @property
    def kind(self) -> str:
        return self.m["kind"]

    def medians(self) -> tuple[float, float] | None:
        if len(self.today) < MIN_SAMPLES or len(self.base) < MIN_SAMPLES:
            return None
        return statistics.median(v for _, v in self.today), statistics.median(self.base)


@dataclass
class _Incident:
    model_id: int | None
    kind: str
    label: str
    start: float
    end: float | None = None
    end_unknown: bool = False  # closed without a recorded end (e.g. its recovered alert was deleted)


def _availability(cycles: list[int]) -> tuple[float | None, int, int]:
    n, ok = len(cycles), sum(cycles)
    return (ok / n * 100 if n else None), n, n - ok


def _incidents(events: list[dict], open_now: set[int]) -> tuple[list[_Incident], list[_Incident]]:
    """Pair alert events into model incidents (open -> recovered / reset) and monitor-side outages.
    `open_now`: models with an incident at this moment, to tell a still-open incident from a lost end."""
    open_by_model: dict[int, _Incident] = {}
    models, outages = [], []
    outage: _Incident | None = None
    for a in events:
        k, mid, ts = a["kind"], a["model_id"], a["ts"]
        if k == "net_down" and outage is None:
            outage = _Incident(None, k, "监控端网络中断", ts)
            outages.append(outage)
        elif k == "net_up" and outage is not None:
            outage.end, outage = ts, None
        elif mid is None:
            continue
        elif k in OPEN_KINDS:
            # A running incident only ever produces "changed" alerts, so another open means the previous one ended
            # without a record (its alert was deleted): stop it here instead of swallowing the new incident.
            if mid in open_by_model:
                prev = open_by_model.pop(mid)
                prev.end, prev.end_unknown = ts, True
            brief = (a.get("brief") or "").split(" · ", 1)
            label = brief[1] if len(brief) > 1 and k == "down" else KIND_NAME[k]
            open_by_model[mid] = _Incident(mid, k, label, ts)
            models.append(open_by_model[mid])
        elif k in ("recovered", "reset") and mid in open_by_model:
            inc = open_by_model.pop(mid)
            inc.end = ts
    for mid, inc in open_by_model.items():
        if mid not in open_now:
            inc.end_unknown = True  # no longer open, but its end was never recorded
    return models, outages


def _overlap(inc: _Incident, start: float, end: float, now: float) -> float:
    if inc.end_unknown:
        return 0.0
    return max(0.0, min(inc.end or now, end) - max(inc.start, start))


def _load(conn: sqlite3.Connection, by_id: dict[int, "_Model"], base_start: float, start: float, end: float):
    """Fill per-model samples / cycles; returns the prior days' cycle outcomes (for the availability baseline)."""
    q = lambda sql, args: conn.execute(sql, args).fetchall()
    for r in q("SELECT model_id, ts, ttft_ms FROM checks WHERE ts >= ? AND ts < ? AND attempt = 0 AND success = 1 "
               "AND interfered = 0 AND net_down = 0 AND ttft_ms IS NOT NULL", (base_start, end)):
        md = by_id.get(r["model_id"])
        if md:
            (md.today.append((r["ts"], r["ttft_ms"])) if r["ts"] >= start else md.base.append(r["ttft_ms"]))
    failed: set[str] = set()
    for r in q("SELECT model_id, cycle_id, MAX(success) AS ok FROM checks WHERE ts >= ? AND ts < ? AND net_down = 0 "
               "GROUP BY model_id, cycle_id", (start, end)):
        md = by_id.get(r["model_id"])
        if md:
            md.cycles.append(r["ok"])
            if not r["ok"]:
                failed.add(r["cycle_id"])
    if failed:
        last: dict[str, dict] = {}
        for r in q("SELECT model_id, cycle_id, attempt, http_status, substr(error, 1, 300) AS error FROM checks "
                   "WHERE ts >= ? AND ts < ? AND net_down = 0 AND success = 0", (start, end)):
            if r["cycle_id"] in failed and (r["cycle_id"] not in last or r["attempt"] > last[r["cycle_id"]]["attempt"]):
                last[r["cycle_id"]] = dict(r)
        for r in last.values():
            if r["model_id"] in by_id:
                by_id[r["model_id"]].failures.append(r)
    return [r["ok"] for r in q("SELECT MAX(success) AS ok FROM checks WHERE ts >= ? AND ts < ? AND net_down = 0 "
                               "GROUP BY cycle_id", (base_start, start))]


def _events(conn: sqlite3.Connection, since: float, until: float) -> list[dict]:
    marks = ", ".join("?" for _ in EVENT_KINDS)
    rows = conn.execute(f"SELECT model_id, ts, kind, brief FROM alerts WHERE ts >= ? AND ts < ? AND kind IN ({marks})",
                        (since, until, *EVENT_KINDS)).fetchall()
    rows = [dict(r) for r in rows]
    rows += [{"model_id": r["model_id"], "ts": r["ts"], "kind": "reset", "brief": ""} for r in conn.execute(
        "SELECT model_id, ts FROM incident_resets WHERE ts >= ? AND ts < ?", (since, until)).fetchall()]
    return sorted(rows, key=lambda r: r["ts"])


def build(db: Database, day: date, settings: dict[str, str], tz: tzinfo, now: float) -> tuple[Notice, str]:
    """The report for one local calendar day. Returns the notice and a one-line summary."""
    start, end = day_bounds(day, tz)
    base_start = start - BASE_DAYS * 86400
    pct = float(settings.get("daily_report_ttft_pct") or 20)
    top = max(1, int(settings.get("daily_report_top_n") or 10))
    policy = IntervalPolicy.from_settings(settings, tz)

    all_models = db.list_models()
    by_id: dict[int, _Model] = {}
    counts = Counter((m["kind"], (m["base_model"] or m["deployment"]).lower(), m["region"]) for m in all_models)
    for m in all_models:
        ident = m["base_model"] or m["deployment"]
        label = f"{ident} · {m['region']}"
        if counts[(m["kind"], ident.lower(), m["region"])] > 1:
            label += f"（{m['resource_name']}）"  # same model twice in one region: tell them apart
        by_id[m["id"]] = _Model(m, label)

    conn = db.read_connection()
    try:
        base_cycles = _load(conn, by_id, base_start, start, end)
        # Events after the day too: an incident still open at 24:00 is closed by a later recovery.
        events = _events(conn, start - 30 * 86400, max(end, now) + 1)
    finally:
        conn.close()
    open_now = {mid for mid, md in by_id.items() if md.m["incident_kind"]}
    incidents, outages = _incidents(events, open_now)
    incidents = [i for i in incidents if i.start < end and (i.end is None or i.end > start)
                 and not (i.end_unknown and i.start < start)]  # an unknown end can't be placed in this day
    outages = [o for o in outages if o.start < end and (o.end is None or o.end > start)]

    # ---- 1. overview ----
    day_cycles = [ok for md in by_id.values() for ok in md.cycles]
    avail, n_cycles, _ = _availability(day_cycles)
    base_avail, _, _ = _availability(base_cycles)
    enabled = sum(1 for m in all_models if m["enabled"])
    opened = sum(1 for i in incidents if i.start >= start)
    recovered = sum(1 for i in incidents if i.end is not None and not i.end_unknown and start <= i.end < end)
    still_open = [md for md in by_id.values() if md.m["incident_kind"]]
    sections: list[tuple[str, list[str]]] = []

    if not n_cycles:
        summary = f"{enabled} 个模型 · 当天没有检测数据。"
        brief = f"{day:%m-%d} 日报 · 无检测数据"
        return _notice(day, tz, summary, sections, pct), brief

    summary = (f"{enabled} 个模型 · 检测 {n_cycles:,} 次 · 可用率 {fmt_avail(avail)}"
               + (f"（前 {BASE_DAYS} 天 {fmt_avail(base_avail)}）" if base_avail is not None else "")
               + f"\n新异常 {opened} · 已恢复 {recovered} · 当前仍未恢复 {len(still_open)}")
    if still_open:
        summary += "（" + "、".join(md.label for md in still_open[:5]) + ("等" if len(still_open) > 5 else "") + "）"
    if outages:
        total = sum(_overlap(o, start, end, now) for o in outages)
        summary += f"\n监控端网络中断 {len(outages)} 次，共 {fmt_minutes(total)}（期间的检测不计入统计）"

    # ---- 2. TTFT changes ----
    slower, faster, short = [], [], 0
    for md in by_id.values():
        if not md.today:
            continue
        meds = md.medians()
        if meds is None:
            short += 1
            continue
        med, base = meds
        ch = change_pct(med, base)
        line = (f"{md.label}  {fmt_ttft(med, md.kind)} {fmt_pct(ch)}"
                f"（P90 {fmt_ttft(p90([v for _, v in md.today]), md.kind)}，前 {BASE_DAYS} 天 {fmt_ttft(base, md.kind)}）")
        if md.kind == "image":
            line = "[图像] " + line
        if ch >= pct:
            slower.append((ch, md.kind, line))
        elif ch <= -pct:
            faster.append((ch, md.kind, line))
    order = lambda items, rev: [line for _, _, line in sorted(items, key=lambda x: (x[1] == "image", -x[0] if rev else x[0]))]
    if slower:
        sections.append((f"TTFT 变慢（中位数较前 {BASE_DAYS} 天升高 ≥{pct:g}%）", _limit(order(slower, True), top)))
    if faster:
        sections.append((f"TTFT 变快（降低 ≥{pct:g}%）", _limit(order(faster, False), top)))
    if not slower and not faster:
        sections.append(("TTFT 波动", [f"无明显波动（所有模型中位数变化都在 ±{pct:g}% 以内）"]))
    if short:
        sections[-1][1].append(f"另有 {short} 个模型样本不足（少于 {MIN_SAMPLES} 个），未参与比较")

    # ---- 3. availability issues ----
    issues = []
    for md in by_id.values():
        a, n, failed = _availability(md.cycles)
        if not failed:
            continue
        reasons = Counter()
        for last in md.failures:
            try:
                reasons[describe(ProbeResult(False, last["http_status"], None, None, last["error"])).label] += 1
            except Exception:
                reasons["检测失败"] += 1
        down = sum(_overlap(i, start, end, now) for i in incidents if i.model_id == md.m["id"])
        main = reasons.most_common(1)[0][0] if reasons else "检测失败"
        line = f"{md.label}  {fmt_avail(a)}  失败 {failed}/{n} 轮  主要：{main}"
        if down:
            line += f"  异常 {fmt_minutes(down)}"
        issues.append((a, line))
    sections.append(("可用性问题", _limit([line for _, line in sorted(issues)], top) if issues else ["全天所有模型都连通正常"]))

    # ---- 4. incident review ----
    if incidents:
        def hm(ts: float) -> str:
            d = datetime.fromtimestamp(ts, tz)
            return f"{d:%H:%M}" if d.date() == day else f"{d:%m-%d %H:%M}"

        lines = []
        for i in sorted(incidents, key=lambda i: i.start):
            md = by_id.get(i.model_id)
            name = md.label if md else "（已删除的模型）"
            if i.end_unknown:
                lines.append(f"{name}  {i.label}  {hm(i.start)} 起，结束时间未知（报警记录可能已被删除）")
            elif i.end is None or i.end >= end:
                lines.append(f"{name}  {i.label}  {hm(i.start)} 起，至 24:00 仍未恢复")
            else:
                lines.append(f"{name}  {i.label}  {hm(i.start)} – {hm(i.end)}（{fmt_minutes(i.end - i.start)}）")
        sections.append(("异常事件", _limit(lines, top)))

    # ---- 5. regions ----
    regions: dict[str, dict] = defaultdict(lambda: {"cycles": [], "today": defaultdict(list), "base": defaultdict(list)})
    for md in by_id.values():
        r = regions[md.m["region"]]
        r["cycles"] += md.cycles
        r["today"][md.kind] += [v for _, v in md.today]
        r["base"][md.kind] += md.base
    region_lines = []
    for name, r in sorted(regions.items(), key=lambda kv: -len(kv[1]["cycles"])):
        a, n, _ = _availability(r["cycles"])
        if not n:
            continue
        parts = [f"{name}  可用率 {fmt_avail(a)}"]
        for kind, kname in (("text", "文本"), ("image", "图像")):
            today, base = r["today"][kind], r["base"][kind]
            if len(today) >= MIN_SAMPLES:
                part = f"{kname} {fmt_ttft(statistics.median(today), kind)}"
                if len(base) >= MIN_SAMPLES:
                    part += f" {fmt_pct(change_pct(statistics.median(today), statistics.median(base)))}"
                parts.append(part)
        region_lines.append("  ".join(parts))
    cross = defaultdict(list)
    for md in by_id.values():
        if len(md.today) >= MIN_SAMPLES:
            cross[(md.kind, md.m["base_model"] or md.m["deployment"])].append(
                (statistics.median(v for _, v in md.today), md.m["region"]))
    spread = []
    for (kind, ident), rows in cross.items():
        if len({region for _, region in rows}) < 2:
            continue
        rows.sort()
        (fast, fr), (slow, sr) = rows[0], rows[-1]
        extra = f"（共 {len(rows)} 处部署）" if len(rows) > 2 else ""
        spread.append((slow / fast if fast else 0,
                       f"{ident}：{fr} 最快 {fmt_ttft(fast, kind)}，{sr} 最慢 {fmt_ttft(slow, kind)}{extra}"))
    if spread:
        region_lines.append("同一模型跨地区：")
        region_lines += ["  " + line for line in _limit([line for _, line in sorted(spread, reverse=True)], top)]
    if region_lines:
        sections.append(("地区对比（TTFT 为中位数，箭头为较前 7 天变化）", region_lines))

    # ---- 6. peak vs off-peak ----
    if policy.enabled:
        lines = []
        for kind, kname in (("text", "文本"), ("image", "图像")):
            peak, off = [], []
            for md in by_id.values():
                if md.kind == kind:
                    for ts, v in md.today:
                        (peak if policy.is_peak(ts) else off).append(v)
            if len(peak) >= MIN_SAMPLES and len(off) >= MIN_SAMPLES:
                p, o = statistics.median(peak), statistics.median(off)
                lines.append(f"{kname}：高峰 {fmt_ttft(p, kind)} / 低谷 {fmt_ttft(o, kind)}（高峰{'慢' if p >= o else '快'} "
                             f"{abs(change_pct(p, o)):.0f}%）")
        if lines:
            hours = f"{settings.get('peak_start')} 至 {settings.get('peak_end')}"
            sections.append((f"高峰 vs 低谷（高峰 {hours}，TTFT 中位数）", lines))

    calm = not slower and not faster and not issues and not incidents and not outages
    if calm:
        summary += f"\n全天正常，TTFT 无明显波动。"
    brief = (f"{day:%m-%d} 日报 · 可用率 {fmt_avail(avail)} · 新异常 {opened}"
             + (f" · TTFT 变慢 {len(slower)}" if slower else "") + (f" · 变快 {len(faster)}" if faster else ""))
    return _notice(day, tz, summary, sections, pct), brief


def _notice(day: date, tz: tzinfo, summary: str, sections: list, pct: float) -> Notice:
    title = f"[模型监控日报] {day:%Y-%m-%d}（周{WEEKDAY[day.weekday()]}）"
    footer = (f"统计时段：{day:%Y-%m-%d} 00:00 至 24:00（{getattr(tz, 'key', tz)}）。TTFT 只统计首次检测成功且未受图像检测干扰的样本，"
              f"与前 {BASE_DAYS} 天中位数比较，变化超过 ±{pct:g}% 才列出；可用率按检测轮次计算，不含监控端断网期间。")
    return Notice("report", title, summary, footer=footer, sections=[(t, list(lines)) for t, lines in sections])
