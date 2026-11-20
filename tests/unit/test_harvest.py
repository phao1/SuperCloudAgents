"""Harvested cases keep the clean base, hidden acceptance and selected split."""

import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from sca.config import Settings
from sca.core.errors import ConflictError, InvalidTaskError
from sca.core.models import Task
from sca.eval.suites import load_suite, select_cases
from sca.evolve.activities import EvolutionActivities
from sca.evolve.harvest import HarvestRequest, harvest, publish_case
from sca.evolve.models import EvolutionConfig


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
         "-c", "user.email=fixture@invalid", "-c", "commit.gpgsign=false", *map(str, args)],
        cwd=repo, check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()


@pytest.fixture
def example(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "main.py").write_text("value = 'original'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "original")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "base")
    (repo / "main.py").write_text("value = 'new commit'\n")
    git(repo, "commit", "-am", "advance main")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", eval_suites_dir=tmp_path / "evals")
    task = Task(id=uuid4(), kind="coding", tenant_id="team", state="FAILED",
                spec={"repo_url": str(repo), "base_ref": "base", "instruction": "Improve parsing",
                      "verify_command": "python verify.py", "verify_commands": ["python -m unittest"]},
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=task), list_events=AsyncMock(return_value=[]),
        create_harvest_candidate=AsyncMock(side_effect=lambda values: {
            **values, "tenant_id": task.tenant_id, "status": "pending"}),
    )
    return settings, task, store, repo, base


async def test_harvest_uses_pinned_mirror_commit_and_publishes_holdout(example):
    settings, task, store, repo, base = example
    directory = settings.task_dir(str(task.id))
    directory.mkdir(parents=True)
    git(directory, "clone", "--mirror", repo, "mirror.git")
    (directory / "workspace").mkdir()
    (directory / "workspace" / "main.py").write_text("untrusted task changes")
    task.spec.update(repo_url="/unavailable-origin", base_ref="main")
    task.result["base_sha"] = base
    body = HarvestRequest(task_id=task.id, split="holdout",
                          acceptance_command="python hidden_harvest_marker.py", notes="review")
    row = await harvest(settings, store, body, "submitter")
    source = settings.data_dir / "harvest" / str(row["id"])
    assert row["status"] == "pending"
    assert row["base_sha"] == base
    assert sorted(p.name for p in source.iterdir()) == ["acceptance", "acceptance.yaml", "repo", "task.yaml"]
    assert (source / "repo/main.py").read_text() == "value = 'original'\n"
    assert not (source / "repo/.git").exists()
    spec = yaml.safe_load((source / "task.yaml").read_text())
    assert spec["split"] == "holdout"
    assert spec["category"] == "harvested"
    assert spec["verify_command"] == task.spec["verify_command"]
    assert spec["verify_commands"] == task.spec["verify_commands"]
    assert "repo_url" not in spec
    assert "hidden_harvest_marker" not in str(spec)
    script, = (source / "acceptance").rglob("*.sh")
    assert "python hidden_harvest_marker.py" in script.read_text()

    publish_case(settings, row)
    publish_case(settings, row)
    suite = load_suite(settings.eval_suites_dir / row["suite"])
    case, = select_cases(suite, "holdout")
    assert case["id"] == row["case_id"]
    assert case["spec"]["instruction"] == "Improve parsing"
    with pytest.raises(ValueError, match="no cases"):
        select_cases(suite, "dev")

    gateway = SimpleNamespace(complete=AsyncMock(side_effect=[
        '{"diagnoses": ["test coverage"]}',
        '[{"hypothesis": "more coverage", "overlay": {"tool_guidance": "Use focused tests"}}]',
    ]))
    evolution_store = SimpleNamespace(
        get_eval_run=AsyncMock(return_value={"variant": {"split": "dev"}, "results": []}),
        get_evolution=AsyncMock(return_value={"journal": []}),
    )
    evolution = EvolutionActivities(
        settings, SimpleNamespace(for_tenant=lambda _: evolution_store), gateway,
    )
    evolution.baseline = AsyncMock(return_value={})
    proposals = await evolution.propose({
        "tenant_id": "eval", "dev_run_id": str(uuid4()), "id": str(uuid4()), "overlay": {},
        "suite_hash": suite["hash"],
        "config": EvolutionConfig(suite=str(settings.eval_suites_dir / row["suite"]),
                                  agent_version="v").model_dump(),
    })
    assert proposals[0]["static_check"]["valid"]
    assert "hidden_harvest_marker" not in str(gateway.complete.call_args_list)
    assert case["id"] not in str(gateway.complete.call_args_list)


async def test_harvest_falls_back_to_source_ref_and_uses_all_verify_commands(example):
    settings, task, store, _, base = example
    task.state = "CANCELLED"
    store.list_events.side_effect = [
        [SimpleNamespace(seq=50, type="review.submitted", payload={"decision": "reject"})], [],
    ]
    row = await harvest(settings, store, HarvestRequest(task_id=task.id, split="dev"), "submitter")
    assert row["base_sha"] == base
    source = settings.data_dir / "harvest" / str(row["id"])
    wrapper, = (source / "acceptance").rglob("check.sh")
    text = wrapper.read_text()
    assert task.spec["verify_command"] in text
    assert task.spec["verify_commands"][0] in text
    assert "set -e" in text


@pytest.mark.parametrize("state,kind,eval_id,error", [
    ("CANCELLED", "coding", None, ConflictError),
    ("SUCCEEDED", "coding", None, ConflictError),
    ("FAILED", "analysis", None, InvalidTaskError),
    ("FAILED", "coding", "eval-run", InvalidTaskError),
])
async def test_harvest_rejects_ineligible_tasks(example, state, kind, eval_id, error):
    settings, task, store, _, _ = example
    task.state, task.kind = state, kind
    task.spec["eval_run_id"] = eval_id
    with pytest.raises(error):
        await harvest(settings, store, HarvestRequest(task_id=task.id, split="dev"), "submitter")
    store.create_harvest_candidate.assert_not_awaited()


async def test_publication_rejects_split_edits_and_suite_symlinks(example):
    settings, task, store, _, _ = example
    row = await harvest(settings, store, HarvestRequest(task_id=task.id, split="holdout"), "submitter")
    (settings.eval_suites_dir).mkdir()
    suite = settings.eval_suites_dir / row["suite"]
    suite.symlink_to(settings.data_dir, target_is_directory=True)
    with pytest.raises(ConflictError, match="路径"):
        publish_case(settings, row)
    suite.unlink()
    spec = settings.data_dir / "harvest" / str(row["id"]) / "task.yaml"
    spec.write_text(spec.read_text().replace("split: holdout", "split: dev"))
    with pytest.raises(ConflictError, match="分组"):
        publish_case(settings, row)
    assert not suite.exists()
