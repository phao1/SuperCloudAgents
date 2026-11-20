import asyncio
import json
import runpy
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from sca.agents.opencode_build import perform_build, records_dir, reserve_build, run_command
from sca.config import Settings
from sca.core.models import AgentOverlay
from sca.eval.suites import load_suite, select_cases
from sca.evolve import solidify

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "deploy/opencode"
VERSION = "2.3.0-sca.1"
OVERLAY = AgentOverlay(system_prompt="请检查测试", tool_descriptions={"read": "", "edit": "Edit"})


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *map(str, args)],
        text=True, timeout=20,
    ).rstrip("\n")


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def source_repo(tmp_path):
    source = tmp_path / "source"
    bare = tmp_path / "opencode.git"
    source.mkdir()
    git(source, "init", "-qb", "main")
    git(source, "apply", "--include=packages/core/src/sca-overlay.ts",
        "--include=packages/core/test/sca-overlay.test.ts", PATCHES / "sca-overlay.patch")
    base = commit(source, "Runtime overlays")
    git(source, "clone", "-q", "--bare", source, bare)
    settings = Settings(_env_file=None, data_dir=tmp_path / "state",
                        opencode_repo_url=str(bare), eval_suites_dir=tmp_path / "evals")
    version = settings.opencode_versions_dir / VERSION
    version.mkdir(parents=True)
    (version / "opencode").write_bytes(b"fixture")
    (version / "meta.json").write_text(json.dumps({
        "version": VERSION, "commit": base, "source_repo": str(bare),
    }))
    return settings, source, bare, base


async def test_upgrade_adds_second_patch_idempotently(source_repo, capsys):
    settings, _, bare, base = source_repo
    upgrade = runpy.run_path(str(ROOT / "scripts/opencode_overlay_upgrade.py"))["upgrade"]
    upgrade.__globals__["get_settings"] = lambda: settings
    await upgrade()
    head = git(bare, "rev-parse", "main")
    assert git(bare, "rev-parse", "main^") == base
    assert git(bare, "show", f"main:{solidify.DEFAULT_OVERLAY}") == "{}"
    await upgrade()
    assert git(bare, "rev-parse", "main") == head
    assert "/v1/agents/opencode/builds" in capsys.readouterr().out


async def test_solidifying_an_existing_embedded_overlay_preserves_its_other_keys(source_repo):
    settings, source, bare, _ = source_repo
    git(source, "apply", PATCHES / "sca-overlay-default.patch")
    previous = {"tool_guidance": "keep", "tool_descriptions": {"read": "old", "shell": "keep"}}
    (source / solidify.DEFAULT_OVERLAY).write_text(json.dumps(previous))
    base = commit(source, "Compiled defaults")
    git(source, "push", "-q", bare, "main")
    champion_id, created_at = uuid4(), datetime.now(UTC)
    args = (settings, champion_id, base, created_at, OVERLAY.model_dump(exclude_none=True),
            source.parent / "publish.log")
    first = await solidify.publish_overlay(*args)
    assert await solidify.publish_overlay(*args) == first
    assert git(bare, "rev-parse", "main") == base
    assert git(bare, "rev-parse", solidify.champion_branch(champion_id)) == first
    assert git(bare, "show", "-s", "--format=%P%n%s", first) == (
        f"{base}\nSolidify champion {champion_id}"
    )
    assert git(bare, "diff", "--name-only", base, first) == solidify.DEFAULT_OVERLAY
    expected = AgentOverlay(system_prompt=OVERLAY.system_prompt, tool_guidance="keep",
                            tool_descriptions={"read": "", "edit": "Edit", "shell": "keep"})
    assert git(bare, "show", f"{first}:{solidify.DEFAULT_OVERLAY}") == expected.canonical_json()


@pytest.fixture
def solidification(source_repo):
    settings, source, bare, base = source_repo
    suite = settings.eval_suites_dir / "suite"
    dev = suite / "cases/dev"
    holdout = suite / "cases/holdout"
    unsplit = suite / "cases/other"
    for case, spec in [(dev, "split: dev"), (holdout, "split: holdout"), (unsplit, "{}")]:
        case.mkdir(parents=True)
        (case / "task.yaml").write_text(spec)
        (case / "acceptance.yaml").write_text("command: pytest")
    (suite / "suite.yaml").write_text("name: suite")
    champion = {"id": uuid4(), "agent_version": VERSION, "model": "test-model",
                "overlay": OVERLAY.model_dump(), "created_at": datetime.now(UTC)}
    store = SimpleNamespace(
        get_champion=AsyncMock(return_value=champion),
        get_champion_eval=AsyncMock(return_value={
            "suite_hash": load_suite(suite)["hash"],
            "variant": {"suite": str(suite), "repeat": 2, "concurrency": 1, "case_budget": .05},
        }),
        create_solidify_eval_runs=AsyncMock(),
        get_eval_run=AsyncMock(return_value={"status": "running"}),
        finish_eval_run=AsyncMock(),
    )
    labels = {"kind": solidify.SOLIDIFY_KIND, "champion_id": str(champion["id"]),
              "source_version": VERSION}
    record = reserve_build(settings, str(uuid4()), "eval", ref=solidify.champion_branch(champion["id"]),
                           base_version=VERSION, labels=labels, notes="Release")
    acts = solidify.ChampionSolidifyActivities(settings, SimpleNamespace(for_tenant=lambda _: store))
    (source / "newer-main").touch()
    main = commit(source, "Unrelated change")
    git(source, "push", "-q", bare, "main")
    return acts, store, record, bare, base, main


@pytest.mark.parametrize("outcome", ["completed", "eval_failed", "legacy_eval_error", "build_failed"])
async def test_workflow_builds_from_pinned_base_then_runs_full_suite(solidification, monkeypatch, outcome):
    acts, store, record, bare, base, main = solidification
    settings = acts.settings
    (settings.opencode_src_cache / "node_modules").mkdir(parents=True)
    evals = {}
    sibling_started, release_sibling, sibling_finished = (asyncio.Event() for _ in range(3))

    async def command(argv, log):
        if argv[0] == "git":
            return await run_command(argv, log)
        assert argv == solidify_build_command
        assert (checkout / solidify.DEFAULT_OVERLAY).read_text() == OVERLAY.canonical_json()
        assert not (checkout / "newer-main").exists()
        if outcome == "build_failed":
            raise RuntimeError("Build failed")
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fake binary")
        return ""

    checkout = settings.data_dir / "opencode/build-work" / record["id"] / "src"
    binary = checkout / f"packages/cli/dist/cli-linux-{settings.container_arch}/bin/opencode"
    from sca.agents.opencode_build import build_command

    solidify_build_command = build_command(settings, checkout, record["version"])

    async def execute_activity(name, request, **kwargs):
        return await {
            "champion_solidify_prepare": acts.prepare,
            "champion_solidify_queue_regression": acts.queue_regression,
            "champion_solidify_finish_regression": acts.finish_regression,
        }[name](request)

    async def execute_child(name, request, *, id, **kwargs):
        if name == "OpenCodeBuildWorkflow":
            assert id == f"opencode-build-{record['id']}"
            assert git(bare, "rev-parse", f"{request['ref']}^") == base
            assert not store.create_solidify_eval_runs.called
            return await perform_build(settings, request, runner=command)
        assert name == "EvalRunWorkflow"
        assert id == f"eval-{request['id']}"
        assert store.create_solidify_eval_runs.await_count == 1
        assert request["split"] is None
        cases = select_cases(load_suite(Path(request["suite"])), request["split"])
        assert {case["id"] for case in cases} == {"dev", "holdout", "other"}
        assert request["budget"] == .30
        version = request["variant"]["agent_version"]
        evals[version] = request
        if outcome == "legacy_eval_error" and version == VERSION:
            raise RuntimeError("Eval failed")
        if outcome == "eval_failed":
            assert kwargs["parent_close_policy"] == solidify.workflow.ParentClosePolicy.ABANDON
            if version == VERSION:
                await sibling_started.wait()
                store.get_eval_run.return_value = {"status": "failed", "reason": "Suite changed"}
                return "failed"
            sibling_started.set()
            await release_sibling.wait()
            sibling_finished.set()
        return "completed"

    monkeypatch.setattr(solidify.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(solidify.workflow, "execute_child_workflow", execute_child)
    monkeypatch.setattr(solidify.workflow, "uuid4", uuid4)
    monkeypatch.setattr(solidify.workflow, "patched", lambda _: outcome != "legacy_eval_error")
    workflow_input = {"record": record, "created_by": "operator"}
    path = records_dir(settings) / f"{record['id']}.json"
    if outcome == "build_failed":
        with pytest.raises(RuntimeError, match="Build failed"):
            await solidify.ChampionSolidifyWorkflow().run(workflow_input)
        assert json.loads(path.read_text())["status"] == "failed"
        store.create_solidify_eval_runs.assert_not_called()
        assert not evals
        return

    async with asyncio.timeout(10):
        try:
            result = await solidify.ChampionSolidifyWorkflow().run(workflow_input)
            assert not sibling_finished.is_set()
        finally:
            release_sibling.set()
        if outcome == "eval_failed":
            await sibling_finished.wait()
    version = record["version"]
    meta = json.loads((settings.opencode_versions_dir / version / "meta.json").read_text())
    assert result["status"] == "succeeded"
    assert result["regression_status"] == ("completed" if outcome == "completed" else "failed")
    assert result["overlay_eval_run_id"] == evals[VERSION]["id"]
    assert result["solidified_eval_run_id"] == evals[version]["id"]
    assert evals[VERSION]["variant"]["agent_overlay"] == json.loads(OVERLAY.canonical_json())
    assert evals[version]["variant"]["agent_overlay"] is None
    assert evals[version]["suite_hash"] == evals[VERSION]["suite_hash"]
    assert meta["base_version"] == VERSION
    assert meta["commit"] == result["ref"] == result["commit"]
    assert meta["labels"] == record["labels"]
    assert git(bare, "rev-parse", "main") == main
    assert json.loads(path.read_text()) == result
    if outcome == "legacy_eval_error":
        store.finish_eval_run.assert_awaited_once_with(evals[VERSION]["id"], "failed")
        assert evals[VERSION]["id"] in result["regression_reason"]
    else:
        store.finish_eval_run.assert_not_called()
    if outcome == "eval_failed":
        assert result["regression_reason"] == f"覆盖版评测 {evals[VERSION]['id']} 失败：Suite changed"
        store.get_eval_run.assert_awaited_once_with(evals[VERSION]["id"])


def test_regression_accepts_verified_legacy_suite_hash(solidification):
    acts, store, record, _, _, _ = solidification
    readme = acts.settings.eval_suites_dir / "suite" / "README.md"
    readme.write_text("Original documentation")
    suite = load_suite(readme.parent)
    champion = store.get_champion.return_value
    evidence = {**store.get_champion_eval.return_value, "suite_hash": suite["legacy_hash"]}
    plan = solidify.regression_plan(acts.settings, record, champion, {}, evidence)
    assert {run["suite_hash"] for run in plan["runs"].values()} == {suite["hash"]}
    readme.write_text("The original digest cannot be verified after this edit")
    with pytest.raises(ValueError, match="suite has changed"):
        solidify.regression_plan(acts.settings, record, champion, {}, evidence)


async def test_changed_suite_fails_before_publishing(solidification):
    acts, store, record, bare, _, _ = solidification
    store.get_champion_eval.return_value["suite_hash"] = "stale"
    with pytest.raises(ApplicationError, match="原始评测套件") as exc:
        await acts.prepare({"record": record})
    assert exc.value.non_retryable
    assert git(bare, "branch", "--list", "sca/champion-*") == ""
    path = records_dir(acts.settings) / f"{record['id']}.json"
    assert json.loads(path.read_text())["status"] == "failed"


async def test_workflow_loads_in_temporal_sandbox():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(solidify.ChampionSolidifyWorkflow)
    )
