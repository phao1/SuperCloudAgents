"""Exercise the prompt loop through the API, real storage, and Git scope check."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from sca.agents.opencode_build import perform_build, reserve_build, run_command
from sca.agents.opencode_prompt import PROMPT_PATH, PROMPT_SUITE, TOOL_GUIDANCE
from sca.api import agents, evals
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskState
from sca.eval.activities import EvalActivities
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


def git(source: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


async def test_prompt_candidate_uses_baseline_commit_and_retests_same_variant(
    store, api_store, tmp_path, monkeypatch,
):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    prompt = repo / PROMPT_PATH
    prompt.parent.mkdir(parents=True)
    prompt.write_text(f"Use the available tools.\n{TOOL_GUIDANCE}\n")
    (repo / "other.txt").write_text("keep\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    cfg = Settings(_env_file=None, data_dir=tmp_path / "data", opencode_repo_url=str(repo))
    installed = cfg.opencode_versions_dir / "2.3.0-sca.1"
    installed.mkdir(parents=True)
    (installed / "opencode").touch()
    (installed / "meta.json").write_text(json.dumps({
        "version": installed.name, "source_repo": str(repo), "commit": base,
    }))
    (cfg.opencode_src_cache / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(agents, "get_settings", lambda: cfg)
    monkeypatch.setattr(evals, "get_settings", lambda: cfg)
    workflows = SimpleNamespace(start_workflow=AsyncMock())
    orch = SimpleNamespace(start_task=AsyncMock(), client=workflows)
    app = create_app(
        store=api_store, orchestrator=orch, object_store=LocalObjectStore(tmp_path / "objects"),
        authenticator=StaticTokenAuthenticator({"admin": "eval:admin"}),
    )
    compiler_calls = []

    async def runner(argv, log):
        if argv[0] == cfg.docker_bin:
            compiler_calls.append(argv)
            source = Path(argv[argv.index("-v") + 1].removesuffix(":/src"))
            binary = source / f"packages/cli/dist/cli-linux-{cfg.container_arch}/bin/opencode"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"test compiler output")
            return ""
        return await run_command(argv, log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer admin"},
    ) as client:
        response = await client.post("/v1/evals/runs", json={
            "suite": PROMPT_SUITE, "budget": 1, "case_budget": .05, "repeat": 2, "concurrency": 2,
            "variant": {"agent_backend": "opencode", "agent_version": installed.name,
                        "model": "deepseek-flash"},
        })
        assert response.status_code == 201, response.text
        run_id = response.json()["id"]
        baseline = workflows.start_workflow.call_args.args[1]
        assert baseline["variant"]["prompt_version"] == "v1"
        task, _ = await store.create_task(
            tenant_id="eval", kind="coding",
            spec={**baseline["variant"], "eval_run_id": run_id, "instruction": "Fix the CSV output"},
            idempotency_key=None,
        )
        await store.append_event(task.id, "agent.command", {"command": "python verify.py", "exit_code": 1})
        await store.append_event(task.id, "acceptance.completed", {"output_tail": "hidden fixture"})
        await store.record_eval_result({
            "run_id": UUID(run_id), "tenant_id": "eval", "case_id": "csv-escape",
            "repeat": 0, "task_id": task.id, "verification_passed": True,
            "acceptance_passed": True, "turns": 2, "tokens": 120, "duration_s": 3, "cost_usd": .01,
        })
        await store.finish_eval_run(run_id, "completed")
        response = await client.post("/v1/agents/opencode/fixes", json={
            "failing_task_id": str(task.id), "target": "system_prompt", "agent_backend": "opencode",
            "extra_instruction": "Avoid rerunning unchanged checks",
        })
        assert response.status_code == 201, response.text
        fix = response.json()
        spec = fix["spec"]
        assert spec["labels"]["prompt_round"] == "1"
        assert spec["base_ref"] == base and spec["model"] == "deepseek-flash"
        assert "Avoid rerunning unchanged checks" in spec["instruction"]
        assert "hidden fixture" not in spec["instruction"]
        assert spec["max_cost_usd"] == baseline["case_budget"]
        assert (await client.post("/v1/agents/opencode/builds", json={"task_id": fix["id"]})).status_code == 422

        prompt.write_text(prompt.read_text() + "Run the relevant check after an edit.\n")
        git(repo, "commit", "-qam", "prompt candidate")
        commit = git(repo, "rev-parse", "HEAD")
        await asyncio.to_thread(subprocess.run, ["bash", "-lc", spec["verify_command"]],
                                cwd=repo, check=True)
        await store.update_result(fix["id"], {
            "commit": commit, "verification_passed": True, "pushed": True, "turns": 1,
        })
        for state in (TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING,
                      TaskState.AWAITING_REVIEW, TaskState.SUCCEEDED):
            await store.set_state(fix["id"], state)
        snapshot = await client.get(f'/v1/agents/opencode/fixes/{fix["id"]}/prompt')
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["verified"] and snapshot.json()["candidate_commit"] == commit
        assert "+Run the relevant check" in snapshot.json()["diff"]
        build = await client.post("/v1/agents/opencode/builds", json={"task_id": fix["id"]})
        assert build.status_code == 202, build.text
        build = await perform_build(cfg, build.json(), runner)
        registered = json.loads((cfg.opencode_versions_dir / build["version"] / "meta.json").read_text())
        assert registered["base_version"] == installed.name
        assert registered["system_prompt_path"] == PROMPT_PATH and len(registered["system_prompt_sha256"]) == 64
        response = await client.post(f'/v1/agents/opencode/builds/{build["id"]}/retest')
        assert response.status_code == 201, response.text
        replay = workflows.start_workflow.call_args.args[1]
        assert replay["source_build_id"] == build["id"]
        assert replay["variant"] == {**baseline["variant"], "agent_version": build["version"]}
        for key in ("suite", "suite_hash", "repeat", "concurrency", "budget", "case_budget"):
            assert replay[key] == baseline[key]
        next_case, _ = await store.create_task(
            tenant_id="eval", kind="coding", idempotency_key=None,
            spec={**replay["variant"], "eval_run_id": replay["id"], "instruction": "Fix the CSV output"},
        )
        await store.append_event(next_case.id, "agent.command",
                                 {"command": "python verify.py", "exit_code": 0})
        await store.record_eval_result({
            "run_id": UUID(replay["id"]), "tenant_id": "eval", "case_id": "csv-escape",
            "repeat": 0, "task_id": next_case.id, "verification_passed": True,
            "acceptance_passed": True, "turns": 2, "tokens": 100, "duration_s": 3, "cost_usd": .009,
        })
        await store.finish_eval_run(replay["id"], "completed")
        second = await client.post("/v1/agents/opencode/fixes", json={
            "failing_task_id": str(next_case.id), "target": "system_prompt",
            "agent_backend": "opencode", "extra_instruction": "Remove redundant verification",
        })
        assert second.status_code == 201, second.text
        second_spec = second.json()["spec"]
        assert second_spec["base_ref"] == commit
        assert second_spec["agent_version"] == build["version"]
        assert second_spec["labels"]["prompt_round"] == "2"
        assert second_spec["labels"]["previous_fix_id"] == fix["id"]
        assert "tokens 120 → 100" in second_spec["instruction"]
        with pytest.raises(ApplicationError, match="Suite changed") as error:
            await EvalActivities(cfg, store).prepare({**replay, "suite_hash": "old-suite"})
        assert error.value.non_retryable
        assert (await store.get_eval_run(replay["id"]))["status"] == "failed"

    (repo / "other.txt").write_text("out of scope\n")
    git(repo, "commit", "-qam", "also change another file")
    forged = reserve_build(
        cfg, str(uuid4()), "eval", ref=git(repo, "rev-parse", "HEAD"),
        base_version=installed.name, labels={"target": "system_prompt"},
    )
    with pytest.raises(RuntimeError, match="scope check"):
        await perform_build(cfg, forged, runner)
    assert len(compiler_calls) == 1
