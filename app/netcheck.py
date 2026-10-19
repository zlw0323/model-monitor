"""Is the monitor itself online? Checked against independent reference URLs, not the monitored endpoints."""

import asyncio
from urllib.parse import urlsplit

import httpx

DEFAULT_URLS = "https://www.cloudflare.com/cdn-cgi/trace,https://www.microsoft.com/,https://www.baidu.com/"
MAX_URLS = 5
TIMEOUT_SECONDS = 8


def parse_urls(raw: str) -> list[str]:
    """Split and validate a comma/newline separated list; raises ValueError with a readable message."""
    urls = [u.strip() for u in (raw or "").replace("\n", ",").split(",") if u.strip()]
    if not urls:
        raise ValueError("至少填写一个参考地址")
    if len(urls) > MAX_URLS:
        raise ValueError(f"最多 {MAX_URLS} 个参考地址")
    for u in urls:
        p = urlsplit(u)
        try:
            p.port  # raises on an invalid port
        except ValueError:
            p = None
        if not p or p.scheme not in ("http", "https") or not p.hostname or len(u) > 300:
            raise ValueError(f"参考地址格式不正确：{u[:60]}")
    return urls


async def check(urls: list[str], transport: httpx.AsyncBaseTransport | None = None,
                first_ok: bool = False) -> tuple[bool, list[str]]:
    """Online if ANY reference answers with any HTTP response; a status code is enough to prove connectivity.
    With first_ok, return as soon as one answers (the others are cancelled and left out of the details),
    so one blocked reference doesn't delay every check."""

    async def one(url: str) -> tuple[bool, str]:
        p = urlsplit(url)
        host = p.hostname + (f":{p.port}" if p.port else "")  # never the userinfo part of the URL
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS + 2):
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as c:
                    async with c.stream("GET", url, headers={"User-Agent": "model-monitor-netcheck"}) as r:
                        status = r.status_code  # the body is never needed
            return True, f"{host}：可访问（HTTP {status}）"
        except Exception as e:
            return False, f"{host}：不可访问（{type(e).__name__}）"

    if not first_ok:
        results = await asyncio.gather(*(one(u) for u in urls))
        return any(ok for ok, _ in results), [msg for _, msg in results]
    tasks = [asyncio.create_task(one(u)) for u in urls]
    try:
        for fut in asyncio.as_completed(tasks):
            ok, msg = await fut
            if ok:
                return True, [msg]
        return False, [t.result()[1] for t in tasks]
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
