"""Local OpenAI-compatible fake model; no credentials or external calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def create_app(latency: float = .01, rpm: int = 1800, reject_every: int = 0) -> FastAPI:
    app = FastAPI()
    arrivals: deque[float] = deque()
    calls = 0

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        nonlocal calls
        payload = await request.json()
        calls += 1
        now = time.monotonic()
        while arrivals and now - arrivals[0] >= 60:
            arrivals.popleft()
        if len(arrivals) >= rpm or (reject_every and calls % reject_every == 0):
            return JSONResponse({"error": {"message": "模拟限流"}}, status_code=429,
                                headers={"Retry-After": "1"})
        arrivals.append(now)
        await asyncio.sleep(latency)
        usage = {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
        base = {"id": "chatcmpl-fake", "object": "chat.completion",
                "created": int(time.time()), "model": payload.get("model", "fake")}
        if not payload.get("stream"):
            return {**base, "choices": [{"index": 0, "message": {"role": "assistant",
                    "content": "你好"}, "finish_reason": "stop"}], "usage": usage}

        async def stream():
            for text in ["你", "好"]:
                chunk = {**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                await asyncio.sleep(latency)
            yield "data: " + json.dumps({**base, "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4200)
    parser.add_argument("--latency", type=float, default=.01)
    parser.add_argument("--rpm", type=int, default=1800)
    parser.add_argument("--reject-every", type=int, default=0)
    args = parser.parse_args()
    uvicorn.run(create_app(args.latency, args.rpm, args.reject_every), host="127.0.0.1",
                port=args.port, access_log=False, log_level="warning")
