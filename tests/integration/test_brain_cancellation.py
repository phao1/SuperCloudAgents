"""Opt in with SCA_TEST_DOCKER_BRAIN=1; uses a fake brain tool and a real container."""
import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest

from sca.agents.opencode_brain import OpenCodeBrainBackend
from sca.config import HOST_ARCH, Settings
from sca.core.models import AgentContext

pytestmark = [pytest.mark.docker, pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('SCA_TEST_DOCKER_BRAIN') != '1', reason='Docker brain cancellation is opt-in')]


async def test_cancel_kills_remote_process_tree(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = root / '.data/opencode/src'
    bun = root / f'.data/tools/bun-linux-{HOST_ARCH}/bun'
    effect = source / 'packages/cli/node_modules/effect'
    if not bun.is_file() or not effect.is_dir():
        pytest.skip('installed Bun and Effect dependencies required')
    settings = Settings(_env_file=None, data_dir=tmp_path, opencode_brain_pool=False)
    binary = settings.docker_bin
    name = 'sca-brain-cancel-' + uuid.uuid4().hex

    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            'timeout', '15', binary, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        assert proc.returncode == 0, 'Docker command failed'
        return out.decode().strip()

    modules = tmp_path / 'node_modules'
    modules.mkdir()
    (modules / 'effect').symlink_to(effect.resolve(), target_is_directory=True)
    shutil.copyfile(root / 'deploy/opencode/brain/driver.ts', tmp_path / 'driver.ts')
    tool = tmp_path / 'fake-brain.ts'
    tool.write_text('''import { ChildProcess } from "effect/process"
import { dockerCommand } from "./driver"
from sca.config import HOST_ARCH
const input = JSON.parse(await Bun.stdin.text())
const command = dockerCommand(ChildProcess.make("sh", ["-c", "env -i sleep 300 & wait"]), input)
if (command._tag !== "StandardCommand") throw new Error("Expected standard command")
const child = Bun.spawn([command.command, ...command.args], { stdout: "ignore", stderr: "ignore" })
await child.exited
''')
    agent = OpenCodeBrainBackend(source, bun, root / 'deploy/opencode/brain/launch.py', 'fake',
                                 settings=settings, gateway_url='http://localhost/v1',
                                 docker_prefix=[binary])
    agent.materialize = lambda version: source
    agent.command = lambda: [str(bun), str(tool)]
    ctx = AgentContext(task_id='cancel', agent_home_host=str(tmp_path / 'home'),
                       model_api_key='fake')
    probe = (
        "import glob\n"
        "count = 0\n"
        "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
        " try: count += open(path, 'rb').read() == b'sleep\\x00300\\x00'\n"
        " except FileNotFoundError: pass\n"
        "print(count)"
    )
    task = None
    try:
        await docker('run', '-d', '--network=none', '--name', name, '--entrypoint', 'sh',
                     os.environ.get('SCA_BRAIN_IMAGE', 'sca-sandbox:latest'),
                     '-c', 'mkdir -p /workspace; exec tail -f /dev/null')

        async def consume():
            async for _ in agent.run_turn(None, name, ctx, 'run fake tool',
                                          session_id=None, timeout_s=60):
                pass

        task = asyncio.create_task(consume())
        async with asyncio.timeout(15):
            while await docker('exec', name, 'python3', '-c', probe) == '0':
                if task.done():
                    await task
                    pytest.fail('Brain exited before tool started')
                await asyncio.sleep(.1)
        async with asyncio.timeout(10):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            while await docker('exec', name, 'python3', '-c', probe) != '0':
                await asyncio.sleep(.1)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await docker('rm', '-f', name)
