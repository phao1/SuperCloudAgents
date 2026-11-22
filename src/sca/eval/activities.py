"""Evaluation uses the public files and tasks APIs under a dedicated tenant token."""

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from uuid import UUID

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio import activity

from sca.config import Settings
from sca.core.model_provider import ModelProviderError
from sca.core.models import TERMINAL_STATES, EventType
from sca.db.repo import Store
from sca.eval.errors import invalid_eval_input, suite_changed
from sca.eval.suites import (
    archive_directory,
    case_metadata,
    load_suite,
    matches_suite_hash,
    select_cases,
)
from sca.eval.trajectory import evaluate
from sca.host import remove_tree
from sca.orchestration.provider_errors import account_failure


async def load_activity_suite(path: Path) -> dict:
    try:
        return await asyncio.to_thread(load_suite, path)
    except (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
        raise invalid_eval_input("Evaluation suite is invalid or incomplete") from None


def check_response(response: httpx.Response) -> None:
    if response.status_code in (400, 401, 403, 404, 413, 415, 422):
        raise invalid_eval_input(f"Evaluation API rejected the request (HTTP {response.status_code})")
    response.raise_for_status()


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCA_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )
    eval_api_token: str = ""


class CaseOutcome(TypedDict):
    cost_usd: float
    errored: bool


class EvalActivities:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def all(self) -> list:
        return [self.prepare, self.submit, self.collect, self.finish]

    def http(self) -> httpx.AsyncClient:
        token = EvalSettings().eval_api_token
        if not token:
            raise invalid_eval_input("SCA_EVAL_API_TOKEN must authenticate the dedicated eval tenant")
        return httpx.AsyncClient(base_url=self.settings.api_url,
                                 headers={"Authorization": f"Bearer {token}"}, timeout=60)

    @activity.defn(name="eval_prepare")
    async def prepare(self, request: dict) -> dict:
        suite = await load_activity_suite(Path(request["suite"]))
        expected_hash = request.get("suite_hash")
        if expected_hash is not None and not matches_suite_hash(suite, expected_hash):
            # Also record it here: histories started before the workflow learned to do so rely on it.
            message = "Suite changed after evaluation was queued"
            await self.store.finish_eval_run(request["id"], "failed", reason=message)
            raise suite_changed(message)
        try:
            cases = select_cases(suite, request.get("split"))
        except ValueError as exc:
            raise invalid_eval_input(str(exc)) from None
        await self.store.create_eval_run({
            "id": UUID(request["id"]), "tenant_id": "eval", "suite": suite["name"],
            "suite_hash": expected_hash or suite["hash"],
            "variant": {**request, "case_metadata": case_metadata(cases)},
            "created_by": "cli", "status": "running",
        })
        await self.store.start_eval_run(request["id"])
        return {"cases": [{**case, "kind": suite.get("kind", "coding"),
                           "suite_hash": suite["hash"],
                           "spec": {**suite.get("defaults", {}), **case["spec"]}}
                          for case in cases]}

    async def git(self, directory: Path, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.autocrlf=false", *args,
            cwd=directory, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await proc.wait():
            raise invalid_eval_input(f"Could not prepare the evaluation fixture (git {args[0]} failed)")

    @activity.defn(name="eval_submit")
    async def submit(self, request: dict) -> str:
        case = request["case"]
        current = await load_activity_suite(Path(request["suite"]))
        if not matches_suite_hash(current, case["suite_hash"]):
            raise suite_changed("Suite changed after evaluation started")
        if not any(item["id"] == case["id"] for item in current["cases"]):
            raise invalid_eval_input("Unknown evaluation case")
        spec = {**case["spec"], **request["variant"], "auto_finish": True,
                "eval_run_id": request["id"], "max_cost_usd": request["case_budget"]}
        # Shared memory would make runs depend on earlier runs and flood the review queue with
        # fixture-specific proposals; a suite or variant must opt in explicitly.
        spec.setdefault("memory_enabled", False)
        if case["kind"] == "coding" and request.get("repeat", 1) > 1:
            spec["labels"] = {**spec.get("labels", {}), "fork_from_snapshot": "true"}
        source = Path(case["path"])
        # Stable retry paths and API keys make an interrupted submit safe to repeat.
        root = self.settings.data_dir.resolve() / "eval" / request["id"] / case["id"]
        root.mkdir(parents=True, exist_ok=True)
        async with self.http() as http:
            async def upload(path: Path) -> str:
                try:
                    if path.is_dir():
                        data = await asyncio.to_thread(archive_directory, path)
                        name = "files.tar.gz"
                    else:
                        data = await asyncio.to_thread(path.read_bytes)
                        name = path.name
                except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                    raise invalid_eval_input("Evaluation fixture files are missing") from None
                response = await http.post("/v1/files", files={"file": (name, data)})
                check_response(response)
                uploaded = response.json()
                if uploaded["tenant_id"] != "eval":
                    raise invalid_eval_input("eval API token must belong to tenant eval")
                return uploaded["id"]

            if case["kind"] == "coding":
                repo = root / "repo.git"
                seed = root / "seed"
                from sca.workspace.cache import RepoCache

                locks = RepoCache(root / "locks")
                async with locks._lock(root / "locks" / "fixture"):
                    if not (root / "ready").exists():
                        if repo.exists():
                            remove_tree(repo)
                        if (source / "repo.bundle").exists():
                            await self.git(root, "clone", "--bare", "-c", "core.autocrlf=false",
                                           str(source / "repo.bundle"), str(repo))
                        else:
                            if not (source / "repo").is_dir():
                                raise invalid_eval_input("Evaluation fixture repository is missing")
                            if seed.exists():
                                remove_tree(seed)
                            shutil.copytree(source / "repo", seed)
                            await self.git(seed, "init", "-b", "main")
                            await self.git(seed, "config", "core.autocrlf", "false")
                            await self.git(seed, "add", ".")
                            await self.git(seed, "-c", "user.name=Eval", "-c", "user.email=eval@invalid",
                                           "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
                            await self.git(root, "clone", "--bare", "-c", "core.autocrlf=false",
                                           str(seed), str(repo))
                        (root / "ready").touch()
                spec["repo_url"] = str(repo)
            else:
                data = source / "data"
                inputs = sorted(data.iterdir()) if data.is_dir() else []
                if not inputs or any(not path.is_file() for path in inputs):
                    raise invalid_eval_input("Analysis data must contain flat input files")
                spec["inputs"] = [await upload(path) for path in inputs]
            spec["acceptance_files"] = await upload(source / "acceptance")
            spec["acceptance_command"] = case["acceptance_command"]
            response = await http.post("/v1/tasks", json={
                "kind": case["kind"], "spec": spec, "priority": "eval",
            }, headers={"Idempotency-Key": f"eval:{request['id']}:{case['id']}:{request['iteration']}"})
            check_response(response)
            task = response.json()
            if task["tenant_id"] != "eval":
                await http.post(f"/v1/tasks/{task['id']}/cancel")
                raise invalid_eval_input("eval API token must belong to tenant eval")
            return task["id"]

    @activity.defn(name="eval_collect")
    async def collect(self, request: dict) -> CaseOutcome | float | None:
        async with self.http() as http:
            response = await http.get(f"/v1/tasks/{request['task_id']}")
            check_response(response)
            task = response.json()
        if task["state"] not in TERMINAL_STATES:
            return None
        result, usage = task["result"], task["usage"]
        provider = ModelProviderError.from_payload(result.get("model_provider_error"))
        account_error = task["state"] == "FAILED" and provider is not None and provider.is_account_error
        events, cursor = [], 0
        async with self.http() as http:
            while True:
                response = await http.get(f"/v1/tasks/{task['id']}/events", params={"after": cursor})
                check_response(response)
                batch = response.json()
                events.extend(batch)
                if len(batch) < 500:
                    break
                cursor = batch[-1]["seq"]
        turns = result.get("turns")
        if turns is None:
            turns = max((event["payload"].get("turn", 0) for event in events
                         if event["type"] == "agent.turn_started"), default=0)
        errored = account_error or (
            task["state"] != "SUCCEEDED"
            and not any(event["type"] == EventType.AGENT_TURN_COMPLETED for event in events)
        )
        trajectory_passed, trajectory_findings = None, []
        if not errored:
            try:
                trajectory_passed, trajectory_findings = evaluate(
                    events, request["case"].get("trajectory_checks", []))
            except ValueError:
                raise invalid_eval_input("Evaluation trajectory data is invalid") from None
        cost = usage.get("cost_micro_usd", 0) / 1e6
        await self.store.record_eval_result({
            "run_id": UUID(request["id"]), "tenant_id": "eval", "case_id": request["case"]["id"],
            "repeat": request["iteration"], "task_id": UUID(task["id"]),
            "acceptance_passed": None if errored else result.get("acceptance_passed", False),
            "trajectory_passed": trajectory_passed, "trajectory_findings": trajectory_findings,
            "verification_passed": None if errored else result.get("verification_passed", False),
            "turns": turns,
            "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "cost_usd": cost,
            "duration_s": (datetime.fromisoformat(task["updated_at"]) -
                           datetime.fromisoformat(task["created_at"])).total_seconds(),
        })
        if account_error and provider is not None:
            raise account_failure(provider)
        if request.get("report_outcome"):
            return {"cost_usd": cost, "errored": errored}
        return cost

    @activity.defn(name="eval_finish")
    async def finish(self, request: dict) -> None:
        await self.store.finish_eval_run(request["id"], request["status"], reason=request.get("reason"))
