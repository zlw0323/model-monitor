"""Fake OpenAI-compatible server for local end-to-end testing.

Run: python -m uvicorn tests.mock_openai:app --port 9100
Behaviour is selected by the `model` (deployment) name:
  fast        ~200 ms TTFT
  slow        ~2.5 s TTFT
  reasoning   streams reasoning_content first
  legacy      rejects max_completion_tokens (only max_tokens)
  down        always HTTP 503
  flaky       fails every other request
  image       images endpoint, ~1 s
"""

import asyncio
import itertools
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
_counter = itertools.count()


def _chunk(delta: dict, finish=None) -> str:
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


@app.post("/v1/chat/completions")
async def chat(req: Request):
    if not req.headers.get("authorization", "").startswith("Bearer "):
        return JSONResponse({"error": {"message": "missing key"}}, 401)
    body = await req.json()
    model = body.get("model")
    if model == "down":
        return JSONResponse({"error": {"message": "Service unavailable"}}, 503)
    if model == "flaky" and next(_counter) % 2 == 0:
        return JSONResponse({"error": {"message": "flaky failure"}}, 500)
    if model == "legacy" and "max_completion_tokens" in body:
        return JSONResponse({"error": {"message": "Unrecognized request argument: max_completion_tokens"}}, 400)
    delay = {"slow": 2.5, "reasoning": 0.8}.get(model, 0.2)

    async def gen():
        yield "data: " + json.dumps({"choices": [], "prompt_filter_results": []}) + "\n\n"
        yield _chunk({"role": "assistant", "content": ""})
        await asyncio.sleep(delay)
        if model == "reasoning":
            yield _chunk({"reasoning_content": "thinking"})
        yield _chunk({"content": "OK"})
        yield _chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/images/generations")
async def images(req: Request):
    await asyncio.sleep(1)
    return {"data": [{"b64_json": "aGVsbG8="}]}
