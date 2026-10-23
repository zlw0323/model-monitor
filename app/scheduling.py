"""Probe scheduling helpers: staggered time slots and a concurrency gate that tracks interference."""

import asyncio
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, tzinfo
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

_GOLDEN = (math.sqrt(5) - 1) / 2
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(raw: str) -> int:
    """'08:30' -> minutes after midnight; raises ValueError with a readable message."""
    m = _HHMM.match((raw or "").strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM（00:00~23:59）：{(raw or '')[:20]}")
    return int(m.group(1)) * 60 + int(m.group(2))


@dataclass(frozen=True)
class IntervalPolicy:
    """Check interval of models that follow the global setting: one interval in the peak window,
    another outside it, or a single interval when the peak schedule is off. Times are local (tz)."""

    enabled: bool
    peak_start: int  # minutes after local midnight
    peak_end: int
    peak_minutes: int
    offpeak_minutes: int
    default_minutes: int
    tz: tzinfo

    @classmethod
    def from_settings(cls, s: dict[str, str], tz: tzinfo) -> "IntervalPolicy":
        def num(key: str, fallback: int) -> int:
            try:
                return max(1, int(s.get(key) or fallback))
            except ValueError:
                return fallback

        try:
            start, end = parse_hhmm(s.get("peak_start", "08:00")), parse_hhmm(s.get("peak_end", "20:00"))
        except ValueError:
            start, end = 8 * 60, 20 * 60
        return cls(
            enabled=str(s.get("peak_schedule_enabled", "1")) == "1" and start != end,
            peak_start=start,
            peak_end=end,
            peak_minutes=num("peak_interval_minutes", 15),
            offpeak_minutes=num("offpeak_interval_minutes", 60),
            default_minutes=num("default_interval_minutes", 30),
            tz=tz,
        )

    def is_peak(self, ts: float) -> bool:
        if not self.enabled:
            return False
        d = datetime.fromtimestamp(ts, self.tz)
        minute = d.hour * 60 + d.minute + d.second / 60
        if self.peak_start < self.peak_end:
            return self.peak_start <= minute < self.peak_end
        return minute >= self.peak_start or minute < self.peak_end  # window crosses midnight

    def interval_at(self, ts: float) -> int:
        if not self.enabled:
            return self.default_minutes
        return self.peak_minutes if self.is_peak(ts) else self.offpeak_minutes

    def max_interval(self) -> int:
        return max(self.peak_minutes, self.offpeak_minutes) if self.enabled else self.default_minutes

    def next_change(self, ts: float) -> float | None:
        """Next time (> ts) the peak window starts or ends; None when the schedule is off."""
        if not self.enabled:
            return None
        day = datetime.fromtimestamp(ts, self.tz).date()
        found = []
        for offset in range(3):
            d = day + timedelta(days=offset)
            for minute in (self.peak_start, self.peak_end):
                at = datetime.combine(d, dtime(minute // 60, minute % 60), tzinfo=self.tz).timestamp()
                if at > ts:
                    found.append(at)
        return min(found) if found else None


def next_run(model_id: int, now: float, policy: IntervalPolicy, fixed_minutes: int | None = None) -> float:
    """Next check time (> now). Fixed-interval models stay on their slot. Others use the interval in force at
    the time; when the next slot would fall in a window with another interval, they move to the new interval's
    staggered slot right after the window boundary, so all models switch together but spread out."""
    if fixed_minutes:
        return next_slot(model_id, fixed_minutes, now)
    plain = next_slot(model_id, policy.interval_at(now), now)
    t = now
    for _ in range(6):
        candidate = next_slot(model_id, policy.interval_at(t), t)
        boundary = policy.next_change(t)
        if boundary is None or candidate < boundary:
            # Intervals longer than their own window can push the slot past several boundaries:
            # never wait longer than the longest configured interval.
            return candidate if candidate <= now + policy.max_interval() * 60 else plain
        t = boundary
    return plain


def resource_key(endpoint: str) -> str:
    """Group endpoints by Azure resource: res.openai.azure.com, res.services.ai.azure.com and
    res.cognitiveservices.azure.com are the same resource "res". Other hosts are grouped by hostname."""
    host = (urlsplit(endpoint if "://" in endpoint else "https://" + endpoint).hostname or "").lower()
    if host.endswith(".azure.com"):
        return "azure:" + host.split(".", 1)[0]
    return host


def next_slot(model_id: int, interval_minutes: int, now: float) -> float:
    """Next run time (> now) on this model's fixed slot within its interval.

    Offsets follow a golden-ratio sequence by model id, so any number of models stays spread
    nearly evenly over the interval without reshuffling existing models when one is added or removed.
    """
    period = max(1, int(interval_minutes)) * 60
    offset = ((model_id * _GOLDEN) % 1.0) * period
    k = math.floor((now - offset) / period) + 1
    return k * period + offset


@dataclass(eq=False)
class Slot:
    kind: str
    host: str
    exclusive: bool
    overlaps: set["Slot"] = field(default_factory=set)
    concurrent: int = 0
    # Overlapped an image probe: large responses competing for bandwidth can inflate TTFT.
    interfered: bool = False


class ProbeGate:
    def __init__(self, limits: Callable[[], dict[str, int]]):
        self._limits = limits
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond: asyncio.Condition | None = None
        self._inflight: set[Slot] = set()
        self._exclusive_waiting = 0
        self._exclusive_active = False

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # asyncio primitives are bound to one loop; tests run each cycle in a fresh loop.
            self._loop, self._cond = loop, asyncio.Condition()
            self._inflight, self._exclusive_waiting, self._exclusive_active = set(), 0, False
        assert self._cond is not None
        return self._cond

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def _can_start(self, kind: str, host: str) -> bool:
        if self._exclusive_active or self._exclusive_waiting:
            return False
        lim = self._limits()
        same_kind = sum(1 for s in self._inflight if s.kind == kind)
        same_host = sum(1 for s in self._inflight if s.host == host)
        return same_kind < lim[kind] and same_host < lim["host"]

    @asynccontextmanager
    async def slot(self, kind: str, host: str, exclusive: bool = False) -> AsyncIterator[Slot]:
        cond = self._condition()
        me = Slot(kind, host, exclusive)
        async with cond:
            if exclusive:
                self._exclusive_waiting += 1
                try:
                    await cond.wait_for(lambda: not self._exclusive_active and not self._inflight)
                finally:
                    self._exclusive_waiting -= 1
                self._exclusive_active = True
            else:
                await cond.wait_for(lambda: self._can_start(kind, host))
            for other in self._inflight:
                other.overlaps.add(me)
                me.overlaps.add(other)
            self._inflight.add(me)
        try:
            yield me
        finally:
            async with cond:
                self._inflight.discard(me)
                if exclusive:
                    self._exclusive_active = False
                me.concurrent = len(me.overlaps)
                me.interfered = any(o.kind == "image" for o in me.overlaps)
                me.overlaps = set()
                cond.notify_all()
