"""Analysis workspace, verification, and artifact activities."""

import asyncio
import mimetypes
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.core.models import EventType, tail
from sca.kernel.activities import (
    CheckpointResult,
    KernelActivities,
    PublishInput,
    StepInput,
    VerifyResult,
    WorkspaceInfo,
)
from sca.kinds.analysis.spec import AnalysisSpec
from sca.kinds.analysis.verify import SCRIPT
from sca.storage.local import key_for
from sca.workspace.git import GitWorkspace


class AnalysisActivities(KernelActivities):
    async def _spec(self, task_id: str) -> AnalysisSpec:
        return AnalysisSpec.model_validate((await self.store.get_task(task_id)).spec)

    def all(self) -> list:
        return [self.prepare, self.checkpoint, self.verify, self.publish]

    @activity.defn(name="analysis.prepare")
    async def prepare(self, task_id: str) -> WorkspaceInfo:
        task = await self.store.get_task(task_id)
        ws = self._workspace(task_id)
        if await self._ensure_state(task_id):
            return WorkspaceInfo(task.result["base_sha"], ws.branch)
        spec = await self._spec(task_id)
        # This temporary repository has never been exposed to an agent.
        with tempfile.TemporaryDirectory() as temp:
            seed = Path(temp) / "seed"
            seed.mkdir()
            (seed / "data").mkdir()
            used: set[str] = set()
            for file_id in spec.inputs:
                row = await self.store.for_tenant(task.tenant_id).get_file(UUID(str(file_id)))
                name = row["name"]
                if Path(name).name != name or name in {".", ".."}:
                    raise ApplicationError("Invalid input filename", non_retryable=True)
                while name in used or name.casefold() == ".git":
                    name = f"{file_id}-{name}"
                used.add(name)
                data = await self.objects.get(f"tenants/{task.tenant_id}/files/{file_id}")
                (seed / "data" / name).write_bytes(data)
            (seed / ".gitattributes").write_text("data/** binary\n")
            trusted = GitWorkspace(Path(temp), "main")
            await trusted._git("-C", str(seed), "init", "-b", "main")
            await trusted._git("-C", str(seed), "add", ".")
            await trusted._git(
                "-C",
                str(seed),
                "-c",
                "user.name=SuperCloudAgents",
                "-c",
                "user.email=agents@supercloud.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "Analysis inputs",
            )
            prepared = await ws.prepare(str(seed), "main")
        for path in (ws.workspace / "data").iterdir():
            path.chmod(0o444)
        info = WorkspaceInfo(prepared.base_sha, prepared.branch)
        await self.store.update_result(task_id, asdict(info))
        await self.store.append_event(
            task_id, EventType.WORKSPACE_PREPARED, asdict(info), dedup_key="workspace.prepared"
        )
        await self._save_state(task_id, 0, info.base_sha)
        return info

    @activity.defn(name="analysis.checkpoint")
    async def checkpoint(self, inp: StepInput) -> CheckpointResult:
        await self._ensure_state(inp.task_id)
        ws = self._workspace(inp.task_id)
        sandbox_id = await self._sandbox_id(inp.task_id)
        exit_event, output = await self._run(
            sandbox_id, ws.commit_argv(f"Analysis turn {inp.turn}"), 90
        )
        if exit_event.exit_code:
            raise ApplicationError(f"Checkpoint failed: {tail(output)}")
        commit = await self.transport.pull_commit(sandbox_id, ws)
        await self.transport.pull_agent_home(sandbox_id, self._task_dir(inp.task_id))
        await self._save_state(inp.task_id, inp.turn, commit)
        task = await self.store.get_task(inp.task_id)
        changed = await ws.changed(task.result["base_sha"])
        await self.store.update_result(inp.task_id, {"commit": commit})
        result = CheckpointResult(commit, changed)
        await self.store.append_event(
            inp.task_id,
            EventType.WORKSPACE_CHECKPOINTED,
            {"turn": inp.turn, **asdict(result)},
            dedup_key=f"turn{inp.turn}.checkpoint",
        )
        return result

    @activity.defn(name="analysis.verify")
    async def verify(self, inp: StepInput) -> VerifyResult:
        if await self._ensure_state(inp.task_id):
            await self.sandbox.destroy(await self._sandbox_id(inp.task_id))
            await self.restore_sandbox(inp)
        task = await self.store.get_task(inp.task_id)
        spec = await self._spec(inp.task_id)
        sandbox_id = await self._sandbox_id(inp.task_id)
        commands = [
            ["python", "-c", SCRIPT, task.result["base_sha"]],
            *[["bash", "-lc", check] for check in spec.checks],
        ]
        runs = await asyncio.gather(*(self._run(sandbox_id, cmd, 1500) for cmd in commands))
        failures = [(ev, output) for ev, output in runs if ev.exit_code]
        log = "\n".join(output for _, output in runs)
        result = VerifyResult(
            not failures,
            failures[0][0].exit_code if failures else 0,
            tail("\n".join(out for _, out in failures) if failures else log),
        )
        data = log.encode()
        name = f"verify-turn{inp.turn}.log"
        uri = await self.objects.put(key_for(inp.task_id, name), data, "text/plain")
        await self.store.add_artifact(inp.task_id, name, "text/plain", uri, len(data))
        await self.store.append_event(
            inp.task_id,
            EventType.VERIFICATION_COMPLETED,
            {"turn": inp.turn, **asdict(result), "log_artifact": name},
            dedup_key=f"turn{inp.turn}.verify",
        )
        return result

    @activity.defn(name="analysis.publish")
    async def publish(self, inp: PublishInput) -> dict:
        await self._ensure_state(inp.task_id)
        task = await self.store.get_task(inp.task_id)
        ws = self._workspace(inp.task_id)
        commit = task.result["commit"]
        # Read committed blobs from the trusted bare mirror, never traverse agent paths.
        entries = await ws._git("--git-dir", str(ws.mirror), "ls-tree", "-rz", commit)
        published = []
        for entry in entries.split("\0"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                continue
            if path not in {
                "analysis.py",
                "output/report.md",
                "output/results.json",
                "output/report.html",
            } and not path.startswith("output/figures/"):
                continue
            if (
                int((await ws._git("--git-dir", str(ws.mirror), "cat-file", "-s", oid)).strip())
                > 50 * 1024 * 1024
            ):
                raise ApplicationError("Artifact exceeds 50 MiB limit", non_retryable=True)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "--git-dir",
                str(ws.mirror),
                "cat-file",
                "blob",
                oid,
                stdout=asyncio.subprocess.PIPE,
            )
            data, _ = await proc.communicate()
            if proc.returncode:
                raise ApplicationError("Could not read artifact blob")
            name = path.removeprefix("output/")
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            uri = await self.objects.put(key_for(inp.task_id, name), data, content_type)
            await self.store.add_artifact(inp.task_id, name, content_type, uri, len(data))
            await self.store.append_event(
                inp.task_id,
                EventType.ARTIFACT_PUBLISHED,
                {"name": name, "size_bytes": len(data)},
                dedup_key=f"turn{inp.turn}.artifact.{name}",
            )
            published.append(name)
        result = {
            "commit": commit,
            "artifacts": published,
            "verification_passed": inp.verification_passed,
        }
        await self.store.update_result(inp.task_id, result)
        return result
