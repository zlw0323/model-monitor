"""Probe scheduling helpers: staggered time slots and a concurrency gate that tracks interference."""

import asyncio
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

_GOLDEN = (math.sqrt(5) - 1) / 2


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
