"""Untrusted plan validation against actual base-commit files."""

import json

import pytest
from pydantic import ValidationError

from sca.kinds.coding.planning import MAX_PLAN_BYTES, validate_plan
from sca.kinds.coding.spec import CodingSpec
from sca.workspace.git import GitWorkspace


def plan_data(paths=None):
    return {
        "split": True,
        "reason": "Independent modules",
        "subtasks": [
            {"name": "one", "instruction": "first", "paths": ["src/one/**"]},
            {"name": "two", "instruction": "second", "paths": paths or ["src/two/**"]},
        ],
    }


async def test_overlap_uses_files_at_base_commit(tmp_path):
    seed = GitWorkspace(tmp_path / "seed", "main")
    seed.task_dir.mkdir()
    await seed._git("init", "-b", "main")
    (seed.task_dir / "src/one").mkdir(parents=True)
    (seed.task_dir / "src/one/a.py").write_text("pass\n")
    await seed._git("add", ".")
    await seed._git(
        "-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-m", "base"
    )
    ws = GitWorkspace(tmp_path / "task", "sca/test")
    base = await ws.prepare(str(seed.task_dir), "main", mirror_only=True)
    files = (
        (
            await ws._git(
                "--git-dir", str(ws.mirror), "ls-tree", "-r", "--name-only", "-z", base.base_sha
            )
        )
        .strip("\0")
        .split("\0")
    )
    assert validate_plan(json.dumps(plan_data()).encode(), files).split
    with pytest.raises(ValueError, match="overlapping"):
        validate_plan(json.dumps(plan_data(["src/**"])).encode(), files)
    assert validate_plan(json.dumps(plan_data(["missing/**"])).encode(), files).split


def test_plan_allows_transitive_prerequisites_to_share_a_write_scope():
    data = plan_data(["src/**"])
    first, last = data["subtasks"]
    last["depends_on"] = ["middle"]
    data["subtasks"] = [first, {
        "name": "middle", "instruction": "update tests", "paths": ["tests/**"], "depends_on": ["one"],
    }, last]
    data["parallelism"] = 1
    assert validate_plan(json.dumps(data).encode(), ["src/one/a.py"]).parallelism == 1
    last["depends_on"] = []
    with pytest.raises(ValueError, match="overlapping"):
        validate_plan(json.dumps(data).encode(), ["src/one/a.py"])


@pytest.mark.parametrize("paths", [["/src/one/new.py"], ["src/one/**"], ["/src/**"]])
def test_plan_rejects_overlapping_new_paths(paths):
    with pytest.raises(ValueError, match="overlapping"):
        validate_plan(json.dumps(plan_data(paths)).encode(), [])


@pytest.mark.parametrize("hint", [0, 9, "2", True])
def test_plan_requires_numeric_parallelism_in_range(hint):
    with pytest.raises(ValidationError, match="parallelism"):
        validate_plan(json.dumps({**plan_data(), "parallelism": hint}).encode(), [])


def test_plan_rejects_shared_paths_for_parallel_subtasks():
    with pytest.raises(ValidationError, match="parallel subtasks cannot declare shared paths"):
        validate_plan(json.dumps({**plan_data(), "shared_paths": ["shared.py"]}).encode(), [])


@pytest.mark.parametrize("data", [b"{", b"[]", b"null", b"{}"])
def test_bad_json_or_shape(data):
    with pytest.raises(ValidationError):
        validate_plan(data, [])


@pytest.mark.parametrize("change", ["many", "name", "duplicate", "one", "paths"])
def test_invalid_subtasks(change):
    data = plan_data()
    if change == "many":
        data["subtasks"] *= 5
    elif change == "name":
        data["subtasks"][0]["name"] = "../escape"
    elif change == "duplicate":
        data["subtasks"][1]["name"] = "one"
    elif change == "one":
        data["subtasks"].pop()
    else:
        data["subtasks"][0]["paths"] = []
    with pytest.raises(ValidationError):
        validate_plan(json.dumps(data).encode(), [])


def test_size_limit():
    with pytest.raises(ValueError, match="64 KiB"):
        validate_plan(b" " * (MAX_PLAN_BYTES + 1), [])


def test_no_split_and_spec_defaults():
    assert not validate_plan(b'{"split":false,"reason":"coupled"}', []).split
    spec = CodingSpec(repo_url="repo", instruction="work")
    assert spec.plan == "none" and not spec.plan_review
    with pytest.raises(ValidationError):
        CodingSpec(
            repo_url="repo",
            instruction="work",
            plan="auto",
            subtasks=[{"name": n, "instruction": n} for n in ("one", "two")],
        )


@pytest.mark.parametrize(
    "outcome",
    [
        "valid",
        "no_split",
        "overlap",
        "bad_json",
        "agent_error",
        "backend_value_error",
        "unreadable",
    ],
)
@pytest.mark.parametrize("version", ["v1", "v-test"])
async def test_planning_activity_records_result_and_always_destroys(tmp_path, monkeypatch, outcome, version):
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sca.core.models import EventType, ExecExit
    from sca.kinds.coding.planning import PlanningActivities
    from sca.kinds.coding.subtask_inputs import PlanningInput

    if version == "v-test":
        import shutil

        from sca.kernel import prompts

        root = tmp_path / "coding/prompts/v-test"
        shutil.copytree(prompts.PROMPTS_ROOT / "coding/prompts/v1", root)
        for file in root.glob("*.txt"):
            file.write_text("VERSION TEST\n" + file.read_text())
        monkeypatch.setattr(prompts, "PROMPTS_ROOT", tmp_path)

    async def local_io(function, *args):
        return function(*args)

    monkeypatch.setattr("sca.kinds.coding.planning.asyncio.to_thread", local_io)

    data = plan_data(["src/**"] if outcome == "overlap" else None)
    if outcome == "no_split":
        data = {"split": False, "reason": "coupled", "subtasks": []}
    encoded = b"{" if outcome == "bad_json" else json.dumps(data).encode()
    store = SimpleNamespace(
        append_event=AsyncMock(),
        add_artifact=AsyncMock(),
        get_task=AsyncMock(return_value=SimpleNamespace(result={"base_sha": "base", "memory_note": "memory note", "intents": {"notes": ["rule note"]}})),
    )
    workspace = SimpleNamespace(
        mirror=tmp_path / "mirror.git", _git=AsyncMock(return_value="src/one/a.py\0")
    )
    spec = CodingSpec(
        repo_url="repo", instruction="task instruction", plan="auto", plan_review=True,
        prompt_version=version
    )
    acts = SimpleNamespace(
        store=store,
        prepare_mirror=AsyncMock(),
        _activity_route=dict,
        _workspace=lambda _: workspace,
        _spec=AsyncMock(return_value=spec),
        sandbox=SimpleNamespace(destroy=AsyncMock()),
        objects=SimpleNamespace(put=AsyncMock(return_value="object://plan")),
    )
    child = SimpleNamespace(
        store=SimpleNamespace(update_result=AsyncMock()),
        _workspace=lambda _: SimpleNamespace(prepare=AsyncMock(), branch="plan"),
        create_sandbox=AsyncMock(return_value="planning-sandbox"),
        run_agent_turn=AsyncMock(),
        _run=AsyncMock(return_value=(ExecExit(exit_code=0), base64.b64encode(encoded).decode())),
        _task_dir=lambda _: tmp_path / "disposable",
    )
    if outcome in {"agent_error", "backend_value_error"}:
        error = ValueError if outcome == "backend_value_error" else RuntimeError
        child.run_agent_turn.side_effect = error("private backend error")
    if outcome == "unreadable":
        child._run.return_value = (ExecExit(exit_code=1), "")
    monkeypatch.setattr("sca.kinds.coding.planning.ChildActivities", lambda *_: child)
    result = await PlanningActivities(acts).plan_task(
        PlanningInput("00000000-0000-0000-0000-000000000001", feedback="review feedback",
                      max_parallel_agents=2)
    )
    acts.sandbox.destroy.assert_awaited_once_with("planning-sandbox")
    prompt = child.run_agent_turn.call_args.args[0].prompt
    assert prompt.startswith("VERSION TEST\n") == (version == "v-test")
    assert "task instruction" in prompt and "review feedback" in prompt
    assert "memory note" in prompt and "rule note" in prompt
    events = [call.args[1] for call in store.append_event.call_args_list]
    assert events[0] == EventType.PLAN_STARTED
    if outcome in {"valid", "no_split"}:
        assert result == data
        assert EventType.PLAN_COMPLETED in events
        completed = store.append_event.call_args_list[events.index(EventType.PLAN_COMPLETED)].args[2]
        assert completed.get("max_parallel_agents") == (2 if outcome == "valid" else None)
        store.add_artifact.assert_awaited_once()
        assert (EventType.REVIEW_REQUESTED in events) == (outcome == "valid")
    else:
        assert result is None and EventType.PLAN_REJECTED in events
        store.add_artifact.assert_not_awaited()
        if outcome == "overlap":
            assert "overlapping" in store.append_event.call_args.args[2]["reason"]
        assert "private backend error" not in str(store.append_event.call_args_list)
