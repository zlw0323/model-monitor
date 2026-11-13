"""Public Microsoft status feeds. No model credentials are sent to these endpoints."""

import asyncio
import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .db import Database
from . import config
from .feishu import Notice

log = logging.getLogger("official_rss")
FEEDS = {
    "azure": ("Azure", "https://rssfeed.azure.status.microsoft/en-us/status/feed/"),
    "m365": ("Microsoft 365", "https://status.cloud.microsoft/api/feed/mac"),
    "power_platform": ("Power Platform", "https://status.cloud.microsoft/api/feed/ppac"),
}
KINDS = {"official_issue", "official_update", "official_recovered"}
MAX_BYTES = 1024 * 1024
MAX_ITEMS = 200
RECENT_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 20
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RESOLVED = re.compile(
    r"^\s*(?:\[\s*)?(?:resolved|mitigated|service restored|post[- ]incident review|pir)\b", re.I
)


class FeedError(ValueError):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "div", "p", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("div", "p", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(text: str) -> str:
    parser = _Text()
    parser.feed(text)
    cleaned = _CONTROL.sub("", "".join(parser.parts).encode("utf-8", "replace").decode("utf-8"))
    return "\n".join(" ".join(line.split()) for line in cleaned.splitlines() if line.strip())


def safe_link(raw: str, source: str) -> str:
    """Links are displayed, never fetched; only link back to the publisher."""
    try:
        u = urlsplit(raw)
        allowed = {"status.cloud.microsoft", "azure.status.microsoft", "status.azure.com"}
        if u.scheme == "https" and u.hostname in allowed and not u.username and not u.password and u.port in (None, 443):
            return raw[:1000]
    except ValueError:
        pass
    return "https://azure.status.microsoft/" if source == "azure" else "https://status.cloud.microsoft/"


@dataclass(frozen=True)
class Item:
    ident: str
    title: str
    body: str
    link: str
    state: str  # active | resolved | normal
    published: float | None
    explicit: bool

    @property
    def fingerprint(self) -> str:
        # Some feeds change pubDate on every poll, even when the notice did not change.
        value = json.dumps([self.title, self.body, self.link, self.state], ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()


def parse_feed(raw: bytes, source: str) -> list[Item]:
    if len(raw) > MAX_BYTES:
        raise FeedError("RSS 内容超过大小限制")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise FeedError("RSS 不是有效的 UTF-8") from e
    # The official feeds use UTF-8. Reject DTD/entities before ElementTree can expand anything.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise FeedError("RSS 禁止包含 DTD 或实体声明")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FeedError("RSS XML 格式不正确") from e
    if root.tag != "rss" or root.find("channel") is None:
        raise FeedError("RSS 缺少 channel")
    nodes = root.findall("./channel/item")
    if len(nodes) > MAX_ITEMS:
        raise FeedError("RSS 通告数量超过限制")
    if source != "azure" and len(nodes) > 1:
        raise FeedError("RSS 服务状态条目结构发生变化，请检查订阅")
    items = []
    identifiers = set()
    for node in nodes:
        title = plain(node.findtext("title") or "")
        body = plain(node.findtext("description") or node.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        ident = (node.findtext("guid") or node.findtext("link") or title).strip()
        if not ident or not title or len(ident) > 2000:
            raise FeedError("RSS 通告缺少有效标识或标题")
        if ident in identifiers:
            raise FeedError("RSS 通告标识重复")
        identifiers.add(ident)
        status = re.sub(r"[\s_-]", "", node.findtext("status") or "").lower()
        explicit = bool(status)
        if status in ("servicedegradation", "degraded", "outage", "active", "investigating"):
            state = "active"
        elif status in ("servicerestored", "resolved", "mitigated"):
            state = "resolved"
        elif status in ("available", "operational"):
            state = "normal"
        elif explicit:
            raise FeedError("RSS 返回了未识别的服务状态")
        else:
            # Azure's incident-only feed has no <status>. Resolution is taken from the title or
            # the current-status paragraph, never from old updates quoted further down the notice.
            current = re.search(r"(?im)^current status:\s*(.+)", body)
            state = "resolved" if _RESOLVED.search(title) or (current and _RESOLVED.search(current[1])) else "active"
        published = None
        date_text = node.findtext("pubDate")
        if date_text:
            try:
                dt = parsedate_to_datetime(date_text)
                if dt.tzinfo is not None:
                    published = dt.timestamp()
            except (ValueError, TypeError, OverflowError):
                raise FeedError("RSS 发布时间无效")
        items.append(Item(ident, title, body, safe_link(node.findtext("link") or "", source),
                          state, published, explicit))
    return items


async def fetch(source: str, etag: str = "", modified: str = "",
                transport: httpx.AsyncBaseTransport | None = None) -> tuple[list[Item] | None, str, str]:
    headers = {"User-Agent": "model-monitor-official-rss", "Accept": "application/rss+xml, application/xml"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    async with asyncio.timeout(TIMEOUT_SECONDS + 5):
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
            async with client.stream("GET", FEEDS[source][1], headers=headers) as resp:
                if resp.status_code == 304:
                    if not etag and not modified:
                        raise FeedError("RSS 未经首次获取就返回 304")
                    return None, etag, modified
                if resp.status_code != 200:
                    raise FeedError(f"RSS 请求失败（HTTP {resp.status_code}）")
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise FeedError("RSS 内容超过大小限制")
                return parse_feed(bytes(chunks), source), resp.headers.get("etag", "")[:1000], resp.headers.get("last-modified", "")[:1000]


def notice(source: str, item: Item, kind: str) -> Notice:
    label = {"official_issue": "官方故障", "official_update": "官方更新", "official_recovered": "官方恢复"}[kind]
    summary = f"{FEEDS[source][0]}：{item.title[:180]}"
    facts = [("来源", FEEDS[source][0])]
    if item.published is not None:
        facts.append(("官方发布时间", datetime.fromtimestamp(item.published, config.APP_TZ).strftime("%Y-%m-%d %H:%M:%S")))
    body = item.body[:6000] + ("…（完整内容见官方链接）" if len(item.body) > 6000 else "")
    return Notice("recovered" if kind == "official_recovered" else "down" if kind == "official_issue" else "test",
                  f"[微软官方通告][{label}] {FEEDS[source][0]}", summary, facts,
                  [body, f"官方详情：{item.link}"],
                  "公共 RSS 通告，非本平台实测故障；不保证覆盖所有地区或部署。")


class Watcher:
    def __init__(self, db: Database, transport: httpx.AsyncBaseTransport | None = None):
        self.db = db
        self.transport = transport
        self._lock = asyncio.Lock()

    def status(self) -> list[dict]:
        settings = self.db.get_settings()
        rows = {r["source"]: r for r in self.db.all("SELECT * FROM official_sources")}
        return [{"source": key, "name": name, "url": url,
                 "enabled": settings.get(f"official_rss_{key}") == "1",
                 "last_checked": rows.get(key, {}).get("last_checked"),
                 "last_success": rows.get(key, {}).get("last_success"),
                 "error": rows.get(key, {}).get("error", ""),
                 "item_count": rows.get(key, {}).get("item_count", 0)}
                for key, (name, url) in FEEDS.items()]

    async def poll(self) -> list[dict]:
        async with self._lock:
            settings = self.db.get_settings()
            for source in FEEDS:
                if settings.get(f"official_rss_{source}") != "1":
                    continue
                previous = self.db.one("SELECT * FROM official_sources WHERE source = ?", (source,)) or {}
                now = time.time()
                try:
                    items, etag, modified = await fetch(source, previous.get("etag", ""), previous.get("modified", ""),
                                                        self.transport)
                except (httpx.HTTPError, TimeoutError, FeedError) as e:
                    error = str(e) if isinstance(e, FeedError) else f"RSS 访问失败（{type(e).__name__}）"
                    log.warning("official RSS %s: %s", source, error)
                    self.db.execute(
                        "INSERT INTO official_sources(source,last_checked,error) VALUES (?,?,?) "
                        "ON CONFLICT(source) DO UPDATE SET last_checked=excluded.last_checked,error=excluded.error",
                        (source, now, error))
                    continue
                if self.db.get_settings().get(f"official_rss_{source}") != "1":
                    continue
                with self.db.transaction():
                    if items is not None:
                        for item in items:
                            if source != "azure":
                                # These two official APIs publish a singleton service-status snapshot.
                                # Its GUID may change with each update; correlate by the service, not GUID.
                                item = replace(item, ident="service-status")
                            self._record(source, item, now)
                    count = len(items) if items is not None else previous.get("item_count", 0)
                    self.db.execute(
                        "INSERT INTO official_sources(source,last_checked,last_success,etag,modified,error,item_count) "
                        "VALUES (?,?,?,?,?,'',?) ON CONFLICT(source) DO UPDATE SET "
                        "last_checked=excluded.last_checked,last_success=excluded.last_success,etag=excluded.etag,"
                        "modified=excluded.modified,error='',item_count=excluded.item_count",
                        (source, now, now, etag, modified, count))
            return self.status()

    def _record(self, source: str, item: Item, now: float) -> None:
        old = self.db.one("SELECT * FROM official_items WHERE source=? AND ident=?", (source, item.ident))
        fingerprint = item.fingerprint
        seen = json.loads(old["seen"]) if old else []
        notified = bool(old and old["notified"])
        kind = None
        if old and old["published"] is not None and item.published is not None and item.published < old["published"]:
            return  # a delayed/cache-stale snapshot, not a new service transition
        transition = bool(old and (old["state"] == "active") != (item.state == "active"))
        if transition:
            if fingerprint in seen and (item.published is None or
                                        (old["published"] is not None and item.published <= old["published"])):
                return
            # Keep prior snapshots across recovery; identical text may recur only at a newer time.
        if fingerprint not in seen or transition:
            if item.state == "active":
                recent = item.published is not None and now - RECENT_SECONDS <= item.published <= now + 300
                if notified:
                    kind = "official_update" if old["state"] == "active" else "official_issue"
                elif item.explicit or recent:
                    kind = "official_issue"
            elif item.state in ("normal", "resolved") and notified and old["state"] == "active":
                kind = "official_recovered"
            if fingerprint not in seen:
                seen.append(fingerprint)
        if kind:
            n = notice(source, item, kind)
            # Store pending delivery and the dedup state in one transaction. A restart still dispatches it.
            lines = [n.summary, *[f"{k}：{v}" for k, v in n.facts], *n.details, n.footer]
            aid = self.db.insert_alert(None, kind, n.title, "\n".join(lines), n.to_json(),
                                       model_name=FEEDS[source][0], brief=n.summary[:240], queued=True)
            self.db.update_alert(aid, {"email_status": "不适用：官方 RSS 仅发送飞书", "feishu_status": "待发送"})
            notified = True
        self.db.execute(
            "INSERT INTO official_items(source,ident,fingerprint,seen,state,notified,title,link,published) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,ident) DO UPDATE SET fingerprint=excluded.fingerprint,seen=excluded.seen,"
            "state=excluded.state,notified=excluded.notified,title=excluded.title,link=excluded.link,"
            "published=COALESCE(excluded.published,official_items.published)",
            (source, item.ident, fingerprint, json.dumps(seen[-100:]), item.state, int(notified), item.title[:200], item.link, item.published))
