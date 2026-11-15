"""Exercise binary subprocess transport and real Git bundles without a container runtime."""

import asyncio
import io
import tarfile
from pathlib import Path

import pytest
from test_workspace import source_repo

from sca.core.models import ExecExit, SandboxSpec
from sca.sandbox._transfer import transfer
from sca.sandbox.k8s import pod_manifest
from sca.workspace.git import GitWorkspace
from sca.workspace.transport import MountTransport, SyncTransport

pytestmark = pytest.mark.integration


async def test_binary_pipe_upload_and_download(tmp_path):
    output = tmp_path / "data"

    async def source():
        for _ in range(100):
            yield bytes(range(256)) * 256

    async for _ in transfer(["sh", "-c", 'cat > "$1"', "test", str(output)], source()):
        pass
    chunks = []
    async for chunk in transfer(["cat", str(output)]):
        assert len(chunk) <= 65536
        chunks.append(chunk)
    assert b"".join(chunks) == bytes(range(256)) * 256 * 100


async def test_binary_pipe_error_and_cancel(tmp_path):
    from sca.core.errors import SandboxError

    with pytest.raises(SandboxError, match="failure"):
        async for _ in transfer(["sh", "-c", "echo failure >&2; exit 7"]):
            pass
    stream = transfer(["sh", "-c", "printf ready; exec sleep 300"])
    assert await anext(stream) == b"ready"
    await asyncio.wait_for(stream.aclose(), 3)


async def test_sync_commit_bundle_and_home(tmp_path):
    repo = await source_repo(tmp_path / "source")
    ws = GitWorkspace(tmp_path / "task", "sca/test")
    prepared = await ws.prepare(str(repo), "main")
    # This checkout has never been exposed to untrusted code; simulate sandbox Git here.
    (ws.workspace / "new").write_bytes(b"\x00\xff")
    proc = await asyncio.create_subprocess_exec(
        *ws.commit_argv("turn"), cwd=ws.workspace, stdout=asyncio.subprocess.DEVNULL
    )
    assert await proc.wait() == 0

    class Provider:
        async def exec(self, sandbox_id, argv, **kwargs):
            argv = [str(tmp_path / "commit.bundle") if a == "/tmp/sca.bundle" else a for a in argv]
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=ws.workspace, stdout=asyncio.subprocess.DEVNULL
            )
            yield ExecExit(exit_code=await proc.wait())

        async def download(self, sandbox_id, path):
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                if path == "/tmp/sca.bundle":
                    tar.add(tmp_path / "commit.bundle", arcname="sca.bundle")
                else:
                    member = tarfile.TarInfo("agent_home/session")
                    member.size = 3
                    tar.addfile(member, io.BytesIO(b"\x00\xff\n"))
            yield archive.getvalue()

    transport = SyncTransport(Provider())
    assert transport.mounts(ws.task_dir) == []
    assert MountTransport(Provider()).mounts(ws.task_dir)[0].target == "/workspace"
    commit = await transport.pull_commit("sandbox", ws)
    assert commit != prepared.base_sha
    assert "GIT binary patch" in await ws.diff(prepared.base_sha)
    await transport.pull_agent_home("sandbox", ws.task_dir)
    assert (ws.task_dir / "agent_home/session").read_bytes() == b"\x00\xff\n"


def test_sync_k8s_volumes_have_no_hostpaths():
    pod = pod_manifest("sync", SandboxSpec(image="test", labels={"sca.state_transport": "sync"}))[
        "spec"
    ]
    assert all("emptyDir" in volume for volume in pod["volumes"])
    assert {mount["mountPath"] for mount in pod["containers"][0]["volumeMounts"]} == {
        "/workspace",
        "/agent_home",
    }
    assert pod["initContainers"][0]["command"][0] == "chown"


async def test_sync_activity_handoff_rebuilds_sandbox(tmp_path, store):
    """Run the activity glue with real Git and pipes, using local trusted test scripts."""
    import shutil

    from temporalio.testing import ActivityEnvironment

    from sca.agents.fake import FakeBackend
    from sca.config import Settings
    from sca.core.models import ExecOutput
    from sca.kinds.coding.activities import CodingActivities, StepInput, TurnInput
    from sca.storage.local import LocalObjectStore
    from sca.workspace.tarsafe import extract

    class Provider:
        def __init__(self):
            self.root = tmp_path / "sandbox"
            self.destroyed = 0

        async def create(self, name, spec):
            assert spec.mounts == []
            for directory in ("workspace", "agent_home", "tmp"):
                (self.root / directory).mkdir(parents=True, exist_ok=True)
            return "sandbox"

        async def destroy(self, sandbox_id):
            self.destroyed += 1
            shutil.rmtree(self.root)

        async def upload(self, sandbox_id, target, archive):
            data = io.BytesIO()
            async for chunk in archive:
                data.write(chunk)
            data.seek(0)
            extract(data, self.root / target.lstrip("/"))

        async def download(self, sandbox_id, path):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as tar:
                tar.add(self.root / path.lstrip("/"), arcname=Path(path).name)
            yield data.getvalue()

        async def exec(self, sandbox_id, argv, **kwargs):
            argv = [
                arg.replace("/agent_home", str(self.root / "agent_home")).replace(
                    "/tmp/sca.bundle", str(self.root / "tmp/sca.bundle")
                )
                for arg in argv
            ]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.root / "workspace",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            for stream, data in (("stdout", out), ("stderr", err)):
                for line in data.decode().splitlines():
                    yield ExecOutput(stream=stream, text=line)
            yield ExecExit(exit_code=proc.returncode)

    source = await source_repo(tmp_path / "source")
    task, _ = await store.create_task(
        tenant_id="default",
        kind="coding",
        idempotency_key=None,
        spec={"repo_url": str(source), "instruction": "change hello"},
    )
    task_id = str(task.id)
    provider = Provider()
    objects = LocalObjectStore(tmp_path / "objects")

    def worker(name):
        return CodingActivities(
            settings=Settings(data_dir=tmp_path / name, state_transport="sync"),
            store=store,
            sandbox=provider,
            agent=FakeBackend(),
            objects=objects,
        )

    first = worker("worker-a")
    env = ActivityEnvironment()
    await env.run(first.prepare_workspace, task_id)
    await env.run(first.create_sandbox, task_id)
    await env.run(
        first.run_agent_turn,
        TurnInput(
            task_id,
            1,
            "```sca-fake\necho after > hello; echo session > /agent_home/session\n```",
        ),
    )
    checkpoint = await env.run(first.checkpoint_workspace, StepInput(task_id, 1))
    shutil.rmtree(tmp_path / "worker-a")
    # Half-done data in the surviving sandbox must be discarded by the next worker.
    (provider.root / "workspace/half-done").write_text("uncommitted")
    second = worker("worker-b")
    await env.run(
        second.run_agent_turn,
        TurnInput(
            task_id,
            2,
            "```sca-fake\ntest ! -f half-done && grep after hello && "
            "grep session /agent_home/session && echo continued > hello\n```",
        ),
    )
    assert provider.destroyed == 1
    assert (provider.root / "workspace/hello").read_text() == "continued\n"
    next_checkpoint = await env.run(second.checkpoint_workspace, StepInput(task_id, 2))
    assert checkpoint.commit != next_checkpoint.commit
    assert "+continued" in await second._workspace(task_id).diff(
        task.result.get("base_sha", checkpoint.commit)
    )
