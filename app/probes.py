"""Probes for Azure AI Foundry (v1 API, /openai/v1) and other OpenAI-compatible endpoints."""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config

TOKEN_PARAMS = ("max_completion_tokens", "max_tokens")
_DEPLOYMENT_RE = re.compile(r"/openai/deployments/([^/]+)/")


@dataclass
class ProbeResult:
    success: bool
    http_status: int | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    error: str | None = None
    # Set when the endpoint rejected the token-limit parameter name we sent.
    rejected_param: str | None = None


def _is_azure(host: str) -> bool:
    return host.lower().endswith(".azure.com")


def build_url(endpoint: str, kind: str) -> str:
    """Turn whatever the user pasted from Foundry into the request URL.

    - Full request URLs (…/chat/completions or …/images/generations, with any query) are used as-is.
    - Azure hosts (*.openai.azure.com, *.services.ai.azure.com, *.cognitiveservices.azure.com) are mapped to
      the v1 API base https://<host>/openai/v1 regardless of the path that was pasted.
    - Other hosts are treated as an OpenAI-compatible base URL (e.g. https://host/v1).
    """
    ep = endpoint.strip()
    if "://" not in ep:
        ep = "https://" + ep
    u = urlsplit(ep)
    path = u.path.rstrip("/")
    suffix = "/chat/completions" if kind == "text" else "/images/generations"
    if path.endswith(suffix):
        return urlunsplit((u.scheme, u.netloc, path, u.query, ""))
    idx = path.find("/openai/v1")
    if idx >= 0:
        base = path[: idx + len("/openai/v1")]
    elif _is_azure(u.hostname or ""):
        base = "/openai/v1"
    else:
        base = path
    return urlunsplit((u.scheme, u.netloc, base + suffix, "", ""))


def deployment_from_endpoint(endpoint: str) -> str | None:
    m = _DEPLOYMENT_RE.search(endpoint)
    return m.group(1) if m else None


def auth_headers(url: str, api_key: str) -> dict[str, str]:
    host = urlsplit(url).hostname or ""
    if _is_azure(host):
        return {"api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _elapsed_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _has_token(chunk: dict) -> bool:
    # Azure sends a first chunk with prompt_filter_results and empty choices, then a role-only delta;
    # neither counts. Reasoning models may stream reasoning text before visible content.
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        for key in ("content", "reasoning_content", "reasoning"):
            v = delta.get(key)
            if isinstance(v, str) and v:
                return True
    return False


def _short(text: str, limit: int = 500) -> str:
    # Lone surrogates (an emoji cut in half by the upstream) can't be stored in SQLite: replace them.
    text = " ".join(text.encode("utf-8", "replace").decode("utf-8").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _param_rejected(status: int, body: str, param: str) -> bool:
    return status in (400, 422) and param in body


async def probe_text(
    client: httpx.AsyncClient, endpoint: str, deployment: str, api_key: str, token_param: str
) -> ProbeResult:
    url = build_url(endpoint, "text")
    body = {
        "model": deployment,
        "messages": [{"role": "user", "content": config.TEXT_PROMPT}],
        "stream": True,
        token_param: config.TEXT_MAX_TOKENS,
    }
    headers = {**auth_headers(url, api_key), "Accept": "text/event-stream"}
    t0 = time.perf_counter()
    status = None
    try:
        async with asyncio.timeout(config.TEXT_TIMEOUT_SECONDS):
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                status = resp.status_code
                if status != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    return ProbeResult(
                        False, status, None, _elapsed_ms(t0), f"HTTP {status}: {_short(text)}",
                        rejected_param=token_param if _param_rejected(status, text, token_param) else None,
                    )
                ttft = None
                finish_reason = None
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        return ProbeResult(
                            False, status, ttft, _elapsed_ms(t0), f"流式错误: {_short(json.dumps(chunk['error'], ensure_ascii=False))}"
                        )
                    if ttft is None and _has_token(chunk):
                        ttft = _elapsed_ms(t0)
                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                total = _elapsed_ms(t0)
                if ttft is None:
                    return ProbeResult(
                        False, status, None, total, f"流式响应结束但没有返回任何 token（finish_reason={finish_reason}）"
                    )
                return ProbeResult(True, status, ttft, total)
    except TimeoutError:
        return ProbeResult(False, status, None, _elapsed_ms(t0), f"超时（>{config.TEXT_TIMEOUT_SECONDS}s）")
    except httpx.HTTPError as e:
        return ProbeResult(False, status, None, _elapsed_ms(t0), f"网络错误: {type(e).__name__}: {_short(str(e))}")


IMAGE_FORMAT_PARAMS = ("output_format", "output_compression")


async def probe_image(client: httpx.AsyncClient, endpoint: str, deployment: str, api_key: str) -> ProbeResult:
    """Image endpoints don't stream by default, so TTFT == time until the full image response arrives."""
    body = {
        "model": deployment,
        "prompt": config.IMAGE_PROMPT,
        "quality": config.IMAGE_QUALITY,
        "size": config.IMAGE_SIZE,
        "output_format": config.IMAGE_OUTPUT_FORMAT,
        "output_compression": config.IMAGE_OUTPUT_COMPRESSION,
    }
    r = await _post_image(client, endpoint, body, api_key)
    if r.rejected_param:
        # Non-OpenAI image models may not know the output format options: retry with plain PNG.
        r = await _post_image(client, endpoint, {k: v for k, v in body.items() if k not in IMAGE_FORMAT_PARAMS}, api_key)
    return r


async def _post_image(client: httpx.AsyncClient, endpoint: str, body: dict, api_key: str) -> ProbeResult:
    url = build_url(endpoint, "image")
    t0 = time.perf_counter()
    status = None
    try:
        async with asyncio.timeout(config.IMAGE_TIMEOUT_SECONDS):
            resp = await client.post(url, json=body, headers=auth_headers(url, api_key))
        status = resp.status_code
        elapsed = _elapsed_ms(t0)
        if status != 200:
            rejected = next((p for p in IMAGE_FORMAT_PARAMS if p in body and _param_rejected(status, resp.text, p)), None)
            return ProbeResult(False, status, None, elapsed, f"HTTP {status}: {_short(resp.text)}", rejected_param=rejected)
        try:
            data = resp.json().get("data") or []
        except ValueError:
            return ProbeResult(False, status, None, elapsed, "响应不是合法 JSON")
        if not data or not (data[0].get("b64_json") or data[0].get("url")):
            return ProbeResult(False, status, None, elapsed, "响应中没有图像数据")
        return ProbeResult(True, status, elapsed, elapsed)
    except TimeoutError:
        return ProbeResult(False, status, None, _elapsed_ms(t0), f"超时（>{config.IMAGE_TIMEOUT_SECONDS}s）")
    except httpx.HTTPError as e:
        return ProbeResult(False, status, None, _elapsed_ms(t0), f"网络错误: {type(e).__name__}: {_short(str(e))}")
