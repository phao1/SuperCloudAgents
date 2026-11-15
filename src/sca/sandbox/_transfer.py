"""Binary subprocess pipes, independent of the line-oriented exec protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sca.core.errors import SandboxError


async def transfer(
    argv: list[str], archive: AsyncIterator[bytes] | None = None
) -> AsyncIterator[bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if archive is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SandboxError(str(exc)) from exc
    assert proc.stdout is not None and proc.stderr is not None

    async def feed() -> None:
        if archive is not None:
            assert proc.stdin is not None
            try:
                async for chunk in archive:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            finally:
                proc.stdin.close()

    writer = asyncio.create_task(feed())
    errors = asyncio.create_task(proc.stderr.read())
    try:
        while chunk := await proc.stdout.read(64 * 1024):
            yield chunk
        await proc.wait()
        err = await errors
        if proc.returncode:
            raise SandboxError(err.decode(errors="replace"))
        await writer
    finally:
        writer.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        await asyncio.gather(writer, errors, return_exceptions=True)
