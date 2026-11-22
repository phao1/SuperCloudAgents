from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from sca.api.authz import authorize
from sca.config import get_settings
from sca.core.models import TERMINAL_STATES, Principal
from sca.sandbox.handd import MAX_FRAME, terminal_socket, unix_address

router = APIRouter()
TERMINAL_EVENTS = {"terminal.open", "terminal.output", "terminal.exit"}


async def connect(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    with unix_address(path) as address:
        return await asyncio.open_unix_connection(address, limit=MAX_FRAME)


@router.get("/tasks/{task_id}/terminal", response_class=StreamingResponse)
async def terminal(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read"))],
) -> StreamingResponse:
    store = request.app.state.store.for_tenant(user.tenant_id)
    await store.get_task(task_id, user.tenant_id)
    if sys.platform == "win32":
        raise HTTPException(409, "实时终端需要 OpenCode 脑手分离模式")
    path = terminal_socket(get_settings().data_dir, str(task_id))

    async def generate() -> AsyncIterator[str]:
        keepalive = 0.0
        while (await store.get_task(task_id, user.tenant_id)).state not in TERMINAL_STATES:
            try:
                async with asyncio.timeout(2):
                    reader, writer = await connect(path)
            except (OSError, TimeoutError):
                if time.monotonic() >= keepalive:
                    yield ": keep-alive\n\n"
                    keepalive = time.monotonic() + 15
                await asyncio.sleep(1)
                continue

            try:
                writer.write(b'{"op":"watch"}\n')
                await writer.drain()
                while True:
                    try:
                        line = await asyncio.wait_for(reader.readline(), 15)
                    except TimeoutError:
                        if (await store.get_task(task_id, user.tenant_id)).state in TERMINAL_STATES:
                            return
                        yield ": keep-alive\n\n"
                        continue
                    if not line:
                        break
                    frame = json.loads(line)
                    name = frame.pop("event", None)
                    if name in TERMINAL_EVENTS:
                        yield f"event: {name}\ndata: {json.dumps(frame)}\n\n"
                    elif frame.get("ready") is True:
                        yield ": connected\n\n"
            except (OSError, ValueError):
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
