"""Evaluation uses the public files and tasks APIs under a dedicated tenant token."""

import asyncio
import shutil
from pathlib import Path
from uuid import UUID

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio import activity

from sca.config import Settings
from sca.core.models import TERMINAL_STATES
from sca.db.repo import Store
from sca.eval.suites import archive_directory, load_suite
from sca.eval.trajectory import evaluate


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_", env_file=".env", extra="ignore")
    eval_api_token: str = ""


class EvalActivities:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def all(self) -> list:
        return [self.prepare, self.submit, self.collect, self.finish]

    def http(self) -> httpx.AsyncClient:
        token = EvalSettings().eval_api_token
        if not token:
            raise ValueError("SCA_EVAL_API_TOKEN must authenticate the dedicated eval tenant")
        return httpx.AsyncClient(base_url=self.settings.api_url,
                                 headers={"Authorization": f"Bearer {token}"}, timeout=60)

    @activity.defn(name="eval_prepare")
    async def prepare(self, request: dict) -> dict:
        suite = await asyncio.to_thread(load_suite, Path(request["suite"]))
        await self.store.create_eval_run({
            "id": UUID(request["id"]), "tenant_id": "eval", "suite": suite["name"],
            "suite_hash": suite["hash"], "variant": request, "created_by": "cli", "status": "running",
        })
        await self.store.start_eval_run(request["id"])
        return {"cases": [{**case, "kind": suite.get("kind", "coding"),
                           "suite_hash": suite["hash"],
                           "spec": {**suite.get("defaults", {}), **case["spec"]}}
                          for case in suite["cases"]]}

    async def git(self, directory: Path, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args,
            cwd=directory, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            raise ValueError(err.decode(errors="replace"))

    @activity.defn(name="eval_submit")
    async def submit(self, request: dict) -> str:
        case = request["case"]
        current = await asyncio.to_thread(load_suite, Path(request["suite"]))
        if current["hash"] != case["suite_hash"]:
            raise ValueError("suite changed after evaluation started")
        spec = {**case["spec"], **request["variant"], "auto_finish": True,
                "eval_run_id": request["id"], "max_cost_usd": request["case_budget"]}
        if case["kind"] == "coding" and request.get("repeat", 1) > 1:
            spec["labels"] = {**spec.get("labels", {}), "fork_from_snapshot": "true"}
        source = Path(case["path"])
        # Stable retry paths and API keys make an interrupted submit safe to repeat.
        root = self.settings.data_dir.resolve() / "eval" / request["id"] / case["id"]
        root.mkdir(parents=True, exist_ok=True)
        async with self.http() as http:
            async def upload(path: Path) -> str:
                if path.is_dir():
                    data = await asyncio.to_thread(archive_directory, path)
                    name = "files.tar.gz"
                else:
                    data = await asyncio.to_thread(path.read_bytes)
                    name = path.name
                response = await http.post("/v1/files", files={"file": (name, data)})
                response.raise_for_status()
                uploaded = response.json()
                if uploaded["tenant_id"] != "eval":
                    raise ValueError("eval API token must belong to tenant eval")
                return uploaded["id"]

            if case["kind"] == "coding":
                repo = root / "repo.git"
                seed = root / "seed"
                from sca.workspace.cache import RepoCache

                locks = RepoCache(root / "locks")
                async with locks._lock(root / "locks" / "fixture"):
                    if not (root / "ready").exists():
                        if repo.exists():
                            shutil.rmtree(repo)
                        if (source / "repo.bundle").exists():
                            await self.git(root, "clone", "--bare", str(source / "repo.bundle"), str(repo))
                        else:
                            if seed.exists():
                                shutil.rmtree(seed)
                            shutil.copytree(source / "repo", seed)
                            await self.git(seed, "init", "-b", "main")
                            await self.git(seed, "add", ".")
                            await self.git(seed, "-c", "user.name=Eval", "-c", "user.email=eval@invalid",
                                           "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
                            await self.git(root, "clone", "--bare", str(seed), str(repo))
                        (root / "ready").touch()
                spec["repo_url"] = str(repo)
            else:
                inputs = sorted((source / "data").iterdir())
                if not inputs or any(not path.is_file() for path in inputs):
                    raise ValueError("analysis data must contain flat input files")
                spec["inputs"] = [await upload(path) for path in inputs]
            spec["acceptance_files"] = await upload(source / "acceptance")
            spec["acceptance_command"] = case["acceptance_command"]
            response = await http.post("/v1/tasks", json={
                "kind": case["kind"], "spec": spec, "priority": "eval",
            }, headers={"Idempotency-Key": f"eval:{request['id']}:{case['id']}:{request['iteration']}"})
            response.raise_for_status()
            task = response.json()
            if task["tenant_id"] != "eval":
                await http.post(f"/v1/tasks/{task['id']}/cancel")
                raise ValueError("eval API token must belong to tenant eval")
            return task["id"]

    @activity.defn(name="eval_collect")
    async def collect(self, request: dict) -> float | None:
        async with self.http() as http:
            response = await http.get(f"/v1/tasks/{request['task_id']}")
            response.raise_for_status()
            task = response.json()
        if task["state"] not in TERMINAL_STATES:
            return None
        from datetime import datetime

        result, usage = task["result"], task["usage"]
        events, cursor = [], 0
        async with self.http() as http:
            while True:
                response = await http.get(f"/v1/tasks/{task['id']}/events", params={"after": cursor})
                response.raise_for_status()
                batch = response.json()
                events.extend(batch)
                if len(batch) < 500:
                    break
                cursor = batch[-1]["seq"]
        turns = result.get("turns")
        if turns is None:
            turns = max((event["payload"].get("turn", 0) for event in events
                         if event["type"] == "agent.turn_started"), default=0)
        trajectory_passed, trajectory_findings = evaluate(
            events, request["case"].get("trajectory_checks", []))
        cost = usage.get("cost_micro_usd", 0) / 1e6
        await self.store.record_eval_result({
            "run_id": UUID(request["id"]), "tenant_id": "eval", "case_id": request["case"]["id"],
            "repeat": request["iteration"], "task_id": UUID(task["id"]),
            "acceptance_passed": result.get("acceptance_passed", False),
            "trajectory_passed": trajectory_passed, "trajectory_findings": trajectory_findings,
            "verification_passed": result.get("verification_passed", False),
            "turns": turns,
            "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "cost_usd": cost,
            "duration_s": (datetime.fromisoformat(task["updated_at"]) -
                           datetime.fromisoformat(task["created_at"])).total_seconds(),
        })
        return cost

    @activity.defn(name="eval_finish")
    async def finish(self, request: dict) -> None:
        await self.store.finish_eval_run(request["id"], request["status"])
