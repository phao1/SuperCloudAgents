import asyncio
import json
import sys
from pathlib import Path

import pytest

from sca.agents.opencode_brain import BrainPool
from sca.config import Settings
from sca.core.errors import AgentTurnError

FAKE = '''
import asyncio,json,os,sys
from pathlib import Path
active=peak=cancelled=0
async def handle(reader,writer):
 global active,peak,cancelled
 tasks={}
 async def turn(r):
  global active,peak
  active+=1;peak=max(peak,active)
  try:
   if r.get("prompt")=="crash": os._exit(9)
   if r.get("prompt")=="bad-id":
    writer.write(b'{"id":"wrong","done":true,"exit_code":0}\\n');return
   if r.get("prompt")=="bad-json":
    writer.write(b'not json\\n');return
   await asyncio.sleep(10 if r.get("prompt")=="sleep" else r.get("delay",0.05))
   for event in [{"type":"step_start","sessionID":"ses_fake","part":{}},
                 {"type":"text","part":{"text":"done"}},
                 {"type":"step_finish","part":{"reason":"stop","tokens":{}}},
                 {"done":True,"exit_code":0}]:
    writer.write((json.dumps({**event,"id":r["id"]})+"\\n").encode())
   await writer.drain()
  finally: active-=1
 try:
  while line:=await reader.readline():
   r=json.loads(line)
   if r["op"]=="ping":
    writer.write((json.dumps({"pong":True,"id":r["id"],"pid":os.getpid(),
                            "peak":peak,"active":active,"cancelled":cancelled,"generation":sys.argv[-1]})+"\\n").encode())
    await writer.drain()
   elif r["op"]=="cancel":
    cancelled+=1
    if r["id"] in tasks: tasks[r["id"]].cancel()
   elif r["op"]=="turn": tasks[r["id"]]=asyncio.create_task(turn(r))
 finally:
  for t in tasks.values(): t.cancel()
  await asyncio.gather(*tasks.values(),return_exceptions=True)
  writer.close()
async def main():
 Path("brain.sock").unlink(missing_ok=True)
 server=await asyncio.start_unix_server(handle,path="brain.sock")
 async with server: await server.serve_forever()
asyncio.run(main())
'''


@pytest.fixture
async def pool(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(FAKE)
    settings = Settings(_env_file=None, data_dir=tmp_path,
                        opencode_brain_pool_concurrency=2,
                        opencode_brain_pool_idle_minutes=0.003)
    instance = BrainPool(settings, [sys.executable, str(script), "source", "bun"])
    yield instance
    await instance.close()


async def collect(pool, **request):
    return [raw async for raw in pool.stream("2.3.0-sca.1", Path("."), request)]


@pytest.mark.asyncio
async def test_protocol_warm_restart_and_key_boundary(pool, tmp_path):
    secret = "fake-turn-scoped-test-credential"
    cold = await collect(pool, model_key=secret)
    server = pool.servers["2.3.0-sca.1"]
    first = await pool.ping(server)
    assert cold[0] == {"pool": "cold"}
    assert cold[-1]["type"] == "step_finish"
    assert (await collect(pool, model_key="other-test-key"))[0] == {"pool": "warm"}
    assert (await pool.ping(server))["pid"] == first["pid"]
    with pytest.raises(AgentTurnError, match="disconnected"):
        await collect(pool, prompt="crash")
    assert (await collect(pool))[0] == {"pool": "cold"}
    assert (await pool.ping(server))["pid"] != first["pid"]
    assert server.directory.stat().st_mode & 0o777 == 0o700
    assert secret not in " ".join(pool.command)
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert secret.encode() not in file.read_bytes()


@pytest.mark.asyncio
async def test_concurrency_bound_and_idle_shutdown(pool):
    await asyncio.gather(*(collect(pool, delay=0.08) for _ in range(5)))
    server = pool.servers["2.3.0-sca.1"]
    assert (await pool.ping(server))["peak"] == 2
    await asyncio.sleep(0.3)
    assert server.process is None
    assert (await collect(pool))[0] == {"pool": "cold"}
    await pool.close()
    assert server.process is None
    with pytest.raises(AgentTurnError, match="closed"):
        await collect(pool)


@pytest.mark.asyncio
async def test_cancel_on_timeout_preserves_server(pool):
    await collect(pool)
    server = pool.servers["2.3.0-sca.1"]
    pid = (await pool.ping(server))["pid"]
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await collect(pool, delay=10)
    await asyncio.sleep(0.03)
    status = await pool.ping(server)
    assert status["pid"] == pid
    assert status["cancelled"] == 1
    assert status["active"] == 0
    assert (await collect(pool))[0] == {"pool": "warm"}


@pytest.mark.asyncio
async def test_pool_maps_events_with_summary(pool, tmp_path):
    from sca.agents.opencode_brain import OpenCodeBrainBackend
    from sca.core.models import AgentContext

    executable = tmp_path / "bun"
    executable.touch()
    agent = OpenCodeBrainBackend(tmp_path, executable, executable, "model", settings=pool.settings,
                                 gateway_url="http://localhost/v1")
    agent.pool = pool
    agent.materialize = lambda version: tmp_path
    agent.cleanup_command = lambda container, marker: ["/bin/true"]
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake")
    events = [e async for e in agent.run_turn(None, "container", ctx, "hello",
                                            session_id=None, timeout_s=5)]
    assert events[-1].data == {"final_message": "done"}
    assert events[-2].data["summary"] == "brain_turn"
    assert events[-2].data["pool"] == "cold"
    assert "fake" not in json.dumps(events[-2].data)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["bad-id", "bad-json"])
async def test_invalid_protocol_rejected(pool, prompt):
    with pytest.raises(AgentTurnError, match="protocol"):
        await collect(pool, prompt=prompt)


@pytest.mark.asyncio
async def test_backend_timeout_cleans_turn_marker(pool, tmp_path):
    from sca.agents.opencode_brain import OpenCodeBrainBackend
    from sca.core.models import AgentContext

    executable = tmp_path / "bun"
    executable.touch()
    agent = OpenCodeBrainBackend(tmp_path, executable, executable, "model", settings=pool.settings,
                                 gateway_url="http://localhost/v1")
    agent.pool = pool
    agent.materialize = lambda version: tmp_path
    markers = []

    def cleanup(container, marker):
        markers.append((container, marker))
        return ["/bin/true"]

    agent.cleanup_command = cleanup
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake")
    with pytest.raises(AgentTurnError, match="timed out"):
        async for _ in agent.run_turn(None, "container", ctx, "sleep", session_id=None, timeout_s=.2):
            pass
    assert len(markers) == 1
    assert markers[0][0] == "container"
    assert len(markers[0][1]) == 32
    await asyncio.sleep(.02)
    assert (await pool.ping(pool.servers["2.3.0-sca.1"]))["cancelled"] == 1
