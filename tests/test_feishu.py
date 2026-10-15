import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.crypto import encrypt
from app.feishu import (
    FeishuError,
    FeishuNotConfigured,
    Notice,
    build_payload,
    gen_sign,
    is_webhook_url,
    mask_webhook,
    send,
)

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/d1234567-0000-0000-0000-abcdefabcdef"
NOTICE = Notice("down", "[模型监控][报警] m 连通性失败", "连通性失败", [("模型名称", "m")], ["[t] 首次检测：失败"], "时区")


def settings(secret=""):
    return {"feishu_webhook_enc": encrypt(HOOK), "feishu_secret_enc": encrypt(secret)}


def test_gen_sign_matches_reference_algorithm():
    # Reference implementation from the Feishu custom-bot documentation.
    ts, secret = 1599360473, "demo"
    expected = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert gen_sign(ts, secret) == expected


def test_payload_card_and_sign():
    p = build_payload(NOTICE, "sec", now=1700000000)
    assert p["timestamp"] == "1700000000" and p["sign"] == gen_sign(1700000000, "sec")
    assert p["msg_type"] == "interactive"
    card = p["card"]
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == NOTICE.title
    text = json.dumps(card, ensure_ascii=False)
    assert "模型名称：m" in text and "首次检测：失败" in text
    assert "sign" not in build_payload(NOTICE, "")


def test_payload_size_guard():
    big = Notice("slow", "t", "s", [], ["x" * 6000] * 4)
    p = build_payload(big, "")
    assert len(json.dumps(p, ensure_ascii=False).encode()) < 20 * 1024


def test_url_helpers():
    assert is_webhook_url(HOOK)
    assert is_webhook_url("https://open.larksuite.com/open-apis/bot/v2/hook/abc")
    assert is_webhook_url("http://127.0.0.1:9100/open-apis/bot/v2/hook/abc")  # local mock
    assert not is_webhook_url("https://example.com/foo")
    assert not is_webhook_url("https://evil.example.com/open-apis/bot/v2/hook/abc")
    assert not is_webhook_url("http://open.feishu.cn/open-apis/bot/v2/hook/abc")
    assert mask_webhook(HOOK) == "https://open.feishu.cn/open-apis/bot/v2/hook/d123****cdef"


def _run(handler, s, delays=(0, 0)):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await send(s, NOTICE, client=c, retry_delays=delays)

    asyncio.run(go())


def test_send_success_with_valid_signature():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        assert str(req.url) == HOOK
        assert abs(int(body["timestamp"]) - time.time()) < 5
        assert body["sign"] == gen_sign(int(body["timestamp"]), "sec")
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": {}})

    _run(handler, settings("sec"))
    assert len(seen) == 1


def test_send_error_code_raises():
    def handler(req):
        return httpx.Response(
            200, json={"code": 19021, "msg": "sign match fail or timestamp is not within one hour from current time"}
        )

    with pytest.raises(FeishuError, match="19021"):
        _run(handler, settings("bad"))


def test_send_retries_on_rate_limit():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, json={"code": 11232, "msg": "frequency limited"})
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    _run(handler, settings())
    assert len(calls) == 3


def test_send_not_configured():
    with pytest.raises(FeishuNotConfigured):
        _run(lambda r: httpx.Response(200), {"feishu_webhook_enc": ""})
