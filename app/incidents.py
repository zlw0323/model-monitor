"""Classify probe failures into readable reasons and stable signatures for alert de-duplication."""

import hashlib
import json
import re
from dataclasses import dataclass

from .probes import ProbeResult

HTTP_LABELS = {
    400: "请求参数错误",
    401: "认证失败（API Key 无效）",
    403: "无权限 / 访问被拒绝",
    404: "部署不存在",
    408: "请求超时",
    409: "请求冲突",
    413: "请求过大",
    422: "请求无法处理",
    429: "限流 / 容量不足",
    500: "服务端内部错误",
    502: "网关错误",
    503: "服务不可用",
    504: "网关超时",
}
# Errors that normally hit every deployment of a resource at once (bad key, network, Azure outage).
RESOURCE_WIDE = {"http401", "http403", "http500", "http502", "http503", "http504", "connect", "timeout", "disconnected"}

_JSON_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
_JSON_CODE = re.compile(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"')
_VOLATILE = re.compile(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,})*|\d+(?:\.\d+)?", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Problem:
    code: str  # http429, timeout, connect, slow, ...
    label: str  # human readable reason
    detail: str  # normalized message excerpt (may be empty)
    signature: str  # stable across repeats of the same problem

    @property
    def resource_wide(self) -> bool:
        return self.code in RESOURCE_WIDE


def _clean(text: str) -> str:
    """Upstream text may carry lone surrogates (an emoji cut in half) or control characters, which can't be
    hashed, stored in SQLite or sent; replace them."""
    return _CONTROL.sub(" ", text.encode("utf-8", "replace").decode("utf-8"))


def _normalize(text: str) -> str:
    """Drop numbers, ids and timestamps, so "retry after 12 seconds" and "after 30 seconds" match."""
    text = _VOLATILE.sub("#", text.lower())
    return " ".join(text.split())[:160]


def _signature(code: str, detail: str) -> str:
    return f"{code}:{hashlib.sha1(detail.encode()).hexdigest()[:10]}" if detail else code


def describe(r: ProbeResult) -> Problem:
    err = _clean(r.error or "")
    if r.http_status and r.http_status != 200:
        code = f"http{r.http_status}"
        label = f"{HTTP_LABELS.get(r.http_status, '请求失败')}（HTTP {r.http_status}）"
        body = err.split(":", 1)[1] if ":" in err else err
        msg = _JSON_MESSAGE.search(body)
        api_code = _JSON_CODE.search(body)
        raw = (api_code.group(1) + " " if api_code else "") + (msg.group(1) if msg else body)
        try:
            raw = json.loads(f'"{raw}"')  # unescape \" and \u sequences from the JSON excerpt
        except ValueError:
            pass
        raw = _clean(raw)
        detail = _normalize(raw)
        return Problem(code, label, raw.strip()[:160], _signature(code, detail))
    if "超时" in err or "Timeout" in err:
        return Problem("timeout", "请求超时", "", "timeout")
    if "ConnectError" in err or "无法连接" in err:
        return Problem("connect", "无法连接（网络或 DNS）", "", "connect")
    if "RemoteProtocolError" in err or "ReadError" in err or "WriteError" in err:
        return Problem("disconnected", "连接被中断", "", "disconnected")
    if "无法解密" in err:
        return Problem("key", "API Key 无法解密（SECRET_KEY 变更？）", "", "key")
    if "没有返回任何 token" in err:
        return Problem("no_output", "流式响应没有输出", "", "no_output")
    if "流式错误" in err:
        detail = _normalize(err)
        return Problem("stream_error", "流式响应中返回错误", err[:160], _signature("stream_error", detail))
    if "图像数据" in err or "合法 JSON" in err:
        return Problem("bad_response", "响应内容异常", err[:160], _signature("bad_response", _normalize(err)))
    detail = _normalize(err)
    return Problem("other", "检测失败", err[:160], _signature("other", detail))


SLOW = Problem("slow", "TTFT 超出预警值", "", "slow")
