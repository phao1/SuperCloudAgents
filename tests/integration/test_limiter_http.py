import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from sca.config import Settings
from sca.gateway.limiter import create_app


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


async def test_real_http_sse_first_chunk_not_buffered(tmp_path):
    upstream = FastAPI()
    release = asyncio.Event()
    seen = []

    @upstream.post("/v1/chat/completions")
    async def completion():
        seen.append(1)

        async def chunks():
            yield b'data: {"choices":[]}\n\n'
            await release.wait()
            yield b'data: {"usage":{"total_tokens":2}}\n\ndata: [DONE]\n\n'

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with serve(upstream) as url:
        settings = Settings(_env_file=None, data_dir=tmp_path, model_gateway_url=url,
                            gateway_test_keys={"local": "tenant"})
        app = create_app(settings)
        async with serve(app) as proxy, httpx.AsyncClient(timeout=5, trust_env=False) as client:
            try:
                async with client.stream("POST", proxy + "/v1/chat/completions",
                                         headers={"Authorization": "Bearer local"},
                                         json={"model": "fake", "stream": True}) as response:
                    assert response.status_code == 200
                    chunks = response.aiter_bytes()
                    async with asyncio.timeout(2):
                        first = await anext(chunks)
                    assert first == b'data: {"choices":[]}\n\n'
                    assert not release.is_set()
                    release.set()
                    rest = b"".join([chunk async for chunk in chunks])
                    assert rest.endswith(b"data: [DONE]\n\n")
                assert len(seen) == 1
                pressure = tmp_path / "run/gateway-pressure.json"
                assert pressure.is_file()
            finally:
                release.set()
