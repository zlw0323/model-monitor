"""Feishu (Lark) group custom-bot webhook notifications."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx

from .crypto import decrypt

MAX_BODY_BYTES = 18 * 1024  # Feishu rejects request bodies over 20 KB.
RATE_LIMIT_CODE = 11232
HEADER_COLOR = {"down": "red", "slow": "orange", "recovered": "green", "test": "blue"}


class FeishuNotConfigured(Exception):
    pass


class FeishuError(Exception):
    pass


@dataclass
class Notice:
    kind: str  # down | slow | recovered | test
    title: str
    summary: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    footer: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Notice":
        d = json.loads(raw)
        d["facts"] = [tuple(x) for x in d.get("facts", [])]
        return cls(**d)


FEISHU_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}  # local mock bot for development only


def is_webhook_url(url: str) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    ok_host = (u.scheme == "https" and host in FEISHU_HOSTS) or (u.scheme in ("http", "https") and host in LOCAL_HOSTS)
    return ok_host and u.path.startswith("/open-apis/bot/v2/hook/")


def mask_webhook(url: str) -> str:
    if not url:
        return ""
    head, _, token = url.rpartition("/")
    return f"{head}/{token[:4]}****{token[-4:]}" if len(token) > 10 else f"{head}/****"


def gen_sign(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _plain(text: str) -> dict:
    return {"tag": "plain_text", "content": text}


def build_card(n: Notice) -> dict:
    elements: list[dict] = [{"tag": "div", "text": _plain(n.summary)}]
    if n.facts:
        elements.append(
            {
                "tag": "div",
                "fields": [{"is_short": True, "text": _plain(f"{k}：{v}")} for k, v in n.facts],
            }
        )
    if n.details:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _plain("\n".join(n.details))})
    if n.footer:
        elements.append({"tag": "note", "elements": [_plain(n.footer)]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": _plain(n.title), "template": HEADER_COLOR.get(n.kind, "blue")},
        "elements": elements,
    }


def build_payload(n: Notice, secret: str, now: int | None = None) -> dict:
    payload: dict = {"msg_type": "interactive", "card": build_card(n)}

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode())

    if size() > MAX_BODY_BYTES and n.details:
        details = [d if len(d) <= 300 else d[:300] + "…" for d in n.details]
        keep = len(details)
        while True:
            hidden = len(details) - keep
            shown = details[:keep] + ([f"…另有 {hidden} 行未显示，见平台“报警记录”"] if hidden else [])
            payload["card"] = build_card(Notice(n.kind, n.title, n.summary, n.facts, shown, n.footer))
            if size() <= MAX_BODY_BYTES or keep == 0:
                break
            keep -= 1
    if secret:
        ts = int(now if now is not None else time.time())
        payload = {"timestamp": str(ts), "sign": gen_sign(ts, secret), **payload}
    return payload


async def send(
    settings: dict[str, str],
    notice: Notice,
    client: httpx.AsyncClient | None = None,
    retry_delays: tuple[float, ...] = (2, 5),
) -> None:
    url = decrypt(settings.get("feishu_webhook_enc", ""))
    if not url:
        raise FeishuNotConfigured("未配置飞书 Webhook")
    secret = decrypt(settings.get("feishu_secret_enc", ""))
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        for attempt in range(len(retry_delays) + 1):
            # Re-sign on every attempt so the timestamp stays fresh.
            resp = await client.post(url, json=build_payload(notice, secret))
            try:
                data = resp.json()
            except ValueError:
                data = {}
            code = data.get("code", data.get("StatusCode"))
            if resp.status_code == 200 and code == 0:
                return
            limited = resp.status_code == 429 or code == RATE_LIMIT_CODE
            if limited and attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
                continue
            msg = data.get("msg") or data.get("StatusMessage") or resp.text[:200]
            raise FeishuError(f"HTTP {resp.status_code}, code={code}, {msg}")
    finally:
        if own:
            await client.aclose()
