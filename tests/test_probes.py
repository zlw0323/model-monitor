import asyncio
import json

import httpx

from app import config
from app.probes import auth_headers, build_url, deployment_from_endpoint, probe_image, probe_text


def test_build_url_azure_variants():
    assert build_url("https://res.openai.azure.com/", "text") == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert build_url("res.openai.azure.com", "image") == "https://res.openai.azure.com/openai/v1/images/generations"
    assert (
        build_url("https://res.services.ai.azure.com/api/projects/p1", "text")
        == "https://res.services.ai.azure.com/openai/v1/chat/completions"
    )
    assert (
        build_url("https://res.cognitiveservices.azure.com/openai/v1/", "text")
        == "https://res.cognitiveservices.azure.com/openai/v1/chat/completions"
    )
    full = "https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2025-01-01-preview"
    assert build_url(full, "text") == full
    assert deployment_from_endpoint(full) == "gpt-4o"


def test_build_url_generic_openai_compatible():
    assert build_url("http://127.0.0.1:9000/v1", "text") == "http://127.0.0.1:9000/v1/chat/completions"
    assert build_url("http://127.0.0.1:9000/v1/", "image") == "http://127.0.0.1:9000/v1/images/generations"


def test_auth_headers():
    assert auth_headers("https://x.openai.azure.com/openai/v1/chat/completions", "k") == {"api-key": "k"}
    assert auth_headers("http://127.0.0.1/v1/chat/completions", "k") == {"Authorization": "Bearer k"}


def _sse(*chunks):
    return [f"data: {json.dumps(c)}\n\n".encode() for c in chunks] + [b"data: [DONE]\n\n"]


class _SlowStream(httpx.AsyncByteStream):
    def __init__(self, parts, delays):
        self.parts, self.delays = parts, delays

    async def __aiter__(self):
        for part, delay in zip(self.parts, self.delays):
            await asyncio.sleep(delay)
            yield part


def test_probe_text_ttft_skips_filter_and_role_chunks():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        parts = _sse(
            {"choices": [], "prompt_filter_results": [{"prompt_index": 0}]},
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"index": 0, "delta": {"content": "OK"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
        return httpx.Response(200, stream=_SlowStream(parts, [0, 0, 0.3, 0, 0]), headers={"content-type": "text/event-stream"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_text(c, "https://r.openai.azure.com", "gpt-5", "KEY", "max_completion_tokens")

    r = asyncio.run(run())
    assert r.success, r.error
    assert r.ttft_ms >= 300
    assert seen["body"]["max_completion_tokens"] == config.TEXT_MAX_TOKENS
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "gpt-5"
    assert "reasoning_effort" not in seen["body"]
    assert seen["headers"]["api-key"] == "KEY"


def test_probe_text_reasoning_content_counts_as_first_token():
    def handler(request):
        return httpx.Response(200, content=b"".join(_sse({"choices": [{"delta": {"reasoning_content": "think"}}]})))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_text(c, "http://h/v1", "ds", "k", "max_tokens")

    assert asyncio.run(run()).success


def test_probe_text_no_tokens_is_failure():
    def handler(request):
        return httpx.Response(200, content=b"".join(_sse({"choices": [{"delta": {}, "finish_reason": "length"}]})))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_text(c, "http://h/v1", "m", "k", "max_tokens")

    r = asyncio.run(run())
    assert not r.success and "length" in r.error


def test_probe_text_param_rejected():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model."}}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_text(c, "http://h/v1", "m", "k", "max_tokens")

    r = asyncio.run(run())
    assert not r.success and r.http_status == 400 and r.rejected_param == "max_tokens"


def test_probe_text_auth_error():
    def handler(request):
        return httpx.Response(401, json={"error": {"code": "401", "message": "Access denied due to invalid key"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_text(c, "https://r.openai.azure.com", "m", "bad", "max_completion_tokens")

    r = asyncio.run(run())
    assert not r.success and r.http_status == 401 and r.rejected_param is None and "invalid key" in r.error


def test_probe_image_success_and_defaults():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": "aGVsbG8="}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_image(c, "https://r.openai.azure.com", "gpt-image-1", "k")

    r = asyncio.run(run())
    assert r.success and r.ttft_ms == r.total_ms
    assert seen["body"] == {"model": "gpt-image-1", "prompt": config.IMAGE_PROMPT}
    assert seen["url"] == "https://r.openai.azure.com/openai/v1/images/generations"


def test_probe_image_empty_data_is_failure():
    def handler(request):
        return httpx.Response(200, json={"data": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe_image(c, "https://r.openai.azure.com", "m", "k")

    assert not asyncio.run(run()).success
