import asyncio
import base64
import sys

import pytest

from sca.sandbox.handd import DAEMON, MAX_PTY_OUTPUT, HanddClient, HanddProcess


@pytest.fixture
async def handd():
    client = HanddClient([sys.executable, "-I", "-u", str(DAEMON)])
    await client.start()
    try:
        yield client
    finally:
        await client.close()


async def read_until(job: HanddProcess, marker: bytes) -> bytes:
    output = b""
    while marker not in output:
        frame = await job.receive()
        assert "exit" not in frame
        output += base64.b64decode(frame["stdout"])
    return output


async def test_pty_controlling_terminal_input_and_resize(handd):
    program = """
import os, sys, termios
with open("/dev/tty", "r") as tty:
    assert all(os.isatty(fd) for fd in (0, 1, 2))
    assert termios.tcgetwinsize(tty.fileno()) == (24, 80)
    attrs = termios.tcgetattr(0)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSANOW, attrs)
    print("ready", flush=True)
    answer = tty.readline()
    print(termios.tcgetwinsize(0), answer.strip(), file=sys.stderr)
sys.exit(7)
"""
    async with asyncio.timeout(5), handd.open_pty(
        [sys.executable, "-c", program], cols=80, rows=24, timeout_s=4,
    ) as job:
        assert await read_until(job, b"ready\r\n") == b"ready\r\n"
        await job.resize(112, 45)
        await job.write(b"interactive input\n")
        assert await read_until(job, b"(45, 112) interactive input\r\n") == (
            b"(45, 112) interactive input\r\n"
        )
        assert await job.receive() == {"id": job.id, "exit": 7, "timed_out": False}


async def test_pty_close_kills_process_and_keeps_connection(handd, tmp_path):
    target = tmp_path / "child-survived"
    program = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
    "import pathlib, sys, time; time.sleep(.3); pathlib.Path(sys.argv[1]).touch()", sys.argv[1]])
print("ready", flush=True)
time.sleep(30)
"""
    async with asyncio.timeout(5), handd.open_pty(
        [sys.executable, "-c", program, str(target)], timeout_s=4,
    ) as job:
        await read_until(job, b"ready")
        await job.close()
        assert (await job.receive())["exit"] == -9
    await asyncio.sleep(.4)
    assert not target.exists()
    assert not handd.pending
    assert [event.exit_code async for event in handd.exec([sys.executable, "-c", "pass"])] == [0]


async def test_pty_timeout_and_spawn_error(handd):
    async with asyncio.timeout(5):
        async with handd.open_pty(
            [sys.executable, "-c", "input()"], timeout_s=.2,
        ) as job:
            assert await job.receive() == {"id": job.id, "exit": 124, "timed_out": True}
        async with handd.open_pty(["/does-not-exist"]) as job:
            assert await job.receive() == {"id": job.id, "exit": 127, "timed_out": False}
        async with handd.open_pty([sys.executable, "-c", "pass"], rows=0) as job:
            assert await job.receive() == {"id": job.id, "exit": 127, "timed_out": False}


async def test_pty_output_cap_drains_and_reports_truncation(handd):
    program = "import sys; sys.stdout.buffer.write(b'x' * int(sys.argv[1]))"
    async with asyncio.timeout(5), handd.open_pty(
        [sys.executable, "-c", program, str(MAX_PTY_OUTPUT + 10)], timeout_s=4,
    ) as job:
        size = 0
        async for frame in job.frames():
            if "stdout" in frame:
                size += len(base64.b64decode(frame["stdout"]))
        assert size == MAX_PTY_OUTPUT
        assert frame == {"id": job.id, "exit": 0, "timed_out": False, "truncated": True}
