"""Frozen v1 output and task-selected template coverage."""

import shutil
from string import Template

import pytest

from sca.kernel import prompts
from sca.kinds.analysis import kind as analysis
from sca.kinds.analysis.spec import AnalysisSpec
from sca.kinds.coding import kind as coding
from sca.kinds.coding import prompts as coding_prompts
from sca.kinds.coding.spec import CodingSpec

PLANNING_V1 = """# Task to plan

$instruction

Read the repository and write only .sca/plan.json. Do not commit or push.

For a task with separable deliverables, plan a directed acyclic graph that lets as many agents
work together as safely possible. Break out prerequisites when they unlock parallel work.
For example, define a data model first, build the API and UI in parallel against that contract,
then run integration work that needs both. Use split=false for small or atomic tasks, such as
a one-file bug fix. Do not create subtasks just to increase their count.

For split=true provide a reason and 2 to 8 subtasks. Each subtask needs:
- name: a unique name of up to 31 lowercase letters, digits and hyphens, starting with a letter or digit.
- instruction: a concrete deliverable and the interfaces it can rely on.
- paths: a nonempty list of repository-relative gitignore-style paths or globs. Prefer exact paths,
  including new files. Subtasks that can run together must have disjoint write scopes. A subtask
  may edit its prerequisites' files if needed. Agents may read files outside their scopes.
- depends_on (optional): prerequisite subtask names from earlier in this list. Omit it or use []
  when a subtask can start immediately. Add edges only for real prerequisites.

Example split plan:
{"split":true,"reason":"Define the contract, then implement both clients of it","parallelism":2,"subtasks":[
 {"name":"model","instruction":"Define the shared record and interface","paths":["/src/model.py"]},
 {"name":"api","instruction":"Expose the record through the API","paths":["/src/api.py"],"depends_on":["model"]},
 {"name":"ui","instruction":"Build the UI against the record contract","paths":["/web/form.js"],"depends_on":["model"]},
 {"name":"tests","instruction":"Cover the API and UI together","paths":["/tests/test_record.py"],"depends_on":["api","ui"]}
]}

The optional parallelism value is your estimate of the maximum useful concurrent agents (1 to 8).
Example single-task plan: {"split":false,"reason":"One focused bug fix","subtasks":[]}
$reviewer_feedback"""

OLD = {'coding/fix': 'The verification command failed after your last change.\n\nCommand: $verify_command\nExit status: $exit_code\n\nEnd of its output:\n\n```\n$output\n```\n\nFind the cause and fix it so the command passes. Do not weaken or delete tests to make it pass.\n\nRules:\n- Work only inside the current directory, which is a git checkout of the repository.\n- Do not commit, push, or change git configuration; the platform records your changes.\n- When you are done, reply with a short summary of what you changed and why.\n', 'coding/planning': PLANNING_V1, 'coding/ci_fix': "The project's CI pipeline failed on your last change.\n\nWhat it reported:\n\n```\n$summary\n```\n\n${where}Find the cause and fix it. Do not weaken or delete tests to make it pass.\n\nRules:\n- Work only inside the current directory, which is a git checkout of the repository.\n- Do not commit, push, or change git configuration; the platform records your changes.\n- When you are done, reply with a short summary of what you changed and why.\n", 'coding/planning_feedback': '\nReviewer feedback:\n$feedback\n', 'coding/verify_none': 'No verification command was given; check your work as you see fit.', 'coding/ci_details': 'Details: $url\n\n', 'coding/subtask': 'Overall task context:\n$instruction\n\nYour subtask ($subtask_name):\n$subtask_instruction', 'coding/feedback': 'A reviewer looked at your changes and asked for the following:\n\n$feedback\n\nMake those changes.\n\nRules:\n- Work only inside the current directory, which is a git checkout of the repository.\n- Do not commit, push, or change git configuration; the platform records your changes.\n- When you are done, reply with a short summary of what you changed and why.\n', 'coding/unresolved': '\nUnmerged subtasks; resolve their patches:\n$details', 'coding/initial': '# Task\n\n$instruction\n\n# Verification\n\n$verification\n\nRules:\n- Work only inside the current directory, which is a git checkout of the repository.\n- Do not commit, push, or change git configuration; the platform records your changes.\n- When you are done, reply with a short summary of what you changed and why.\n', 'coding/unresolved_entry': '- $subtask_name ($status): /workspace/.sca/subtask-$subtask_name.patch', 'coding/verify_single': 'Your work will be accepted when this command exits with status 0, run from the repository root:\n\n    $command\n\nRun it yourself before you finish.', 'coding/verify_multiple': 'Your work will be accepted when every one of these commands exits with status 0, each run from the repository root:\n\n$listing\n\nRun them yourself before you finish.', 'analysis/fix': 'Verification failed (exit $exit_code). Fix the analysis and outputs.\n$output', 'analysis/feedback': 'Revise the analysis following this reviewer feedback:\n$feedback', 'analysis/initial': 'Answer this question using the input files in data/:\n$question\n\nDo not modify data/. Write analysis.py, a deterministic Python entry point that reads\nthose inputs and regenerates output/report.md, output/results.json (the key numerical\nresults in the report), and any figures under output/figures/. Reference figures using\nrelative Markdown image links. Never invent numerical results. The report must be\nnonempty. Verification reruns python analysis.py in a clean checkout, removes previous\noutputs, and compares report.md and results.json byte for byte. Use stable ordering and\nformatting. Additional checks: $checks.\nRequested report format: $report_format; always provide report.md\nfor verification and review; also provide output/report.html when HTML is requested.\n'}


@pytest.mark.parametrize("key", sorted(OLD))
def test_v1_literal_output(key):
    kind, name = key.split("/")
    spec = {"_kind": kind, "prompt_version": "v1", "instruction": "work",
            "question": "count", "checks": ["test x"], "report_format": "html",
            "verify_command": "pytest"}
    values = {"exit_code": 2, "output": "error", "feedback": "change",
              "summary": "CI error", "where": "Details: https://ci.invalid/1\n\n",
              "reviewer_feedback": "\nReviewer feedback:\nchange\n",
              "subtask_name": "one", "subtask_instruction": "child",
              "url": "https://ci.invalid/1", "details": "patches", "status": "conflict",
              "command": "pytest", "listing": "    pytest"}
    if name.startswith("verify_"):
        # Verification paragraphs are rendered through the initial prompt.
        commands = {"verify_none": [], "verify_single": ["pytest"],
                    "verify_multiple": ["pytest", "ruff"]}[name]
        spec.pop("verify_command")
        spec["verify_commands"] = commands
        values["listing"] = "\n".join(f"    {c}" for c in commands)
        expected = Template(OLD[key]).substitute({**spec, **values})
        initial = prompts.render_prompt(spec, "initial")
        assert expected in initial
    else:
        values["verification"] = Template(OLD["coding/verify_single"]).substitute(command="pytest")
        expected = Template(OLD[key]).substitute({**spec, **values})
        assert prompts.render_prompt(spec, name, **values) == expected


@pytest.fixture
def alternate_prompts(tmp_path, monkeypatch):
    for kind in ("coding", "analysis"):
        root = tmp_path / kind / "prompts"
        shutil.copytree(prompts.PROMPTS_ROOT / kind / "prompts" / "v1", root / "v1")
        shutil.copytree(root / "v1", root / "v-test")
        for file in (root / "v-test").glob("*.txt"):
            file.write_text("VERSION TEST\n" + file.read_text())
    monkeypatch.setattr(prompts, "PROMPTS_ROOT", tmp_path)
    return tmp_path


async def test_all_activity_templates_switch(alternate_prompts):
    spec = CodingSpec(repo_url="x", instruction="work", prompt_version="v-test").model_dump()
    assert (await coding.initial_prompt(spec)).startswith("VERSION TEST\n")
    assert (await coding.fix_prompt(spec, 1, "error")).startswith("VERSION TEST\n")
    assert (await coding.feedback_prompt("change", [], "v-test")).startswith("VERSION TEST\n")
    assert coding_prompts.planning_prompt("work", "change", "v-test").startswith("VERSION TEST\n")
    assert (await coding.ci_fix_prompt("error", None, "v-test")).startswith("VERSION TEST\n")
    child = await coding.subtask_prompt(spec, {"name": "one", "instruction": "child"})
    assert child.count("VERSION TEST\n") == 3  # initial, wrapper, verification
    spec = AnalysisSpec(question="count", inputs=["12345678-1234-1234-1234-123456789abc"],
                        prompt_version="v-test").model_dump()
    assert (await analysis.initial_prompt(spec)).startswith("VERSION TEST\n")
    assert (await analysis.fix_prompt(spec, 1, "error")).startswith("VERSION TEST\n")
    assert (await analysis.feedback_prompt("change", [], "v-test")).startswith("VERSION TEST\n")


def test_subtask_v1_old_wrapper():
    spec = {"instruction": "  parent\n", "prompt_version": "v1"}
    subtask = {"name": "one", "instruction": " child\n"}
    assert coding_prompts.subtask_prompt(spec, subtask) == (
        "Overall task context:\n  parent\n\n\nYour subtask (one):\n child\n"
    )


@pytest.mark.parametrize("kind", ["coding", "analysis"])
def test_missing_version_fails_at_validation(kind, alternate_prompts):
    model, fields = (CodingSpec, {"repo_url": "x", "instruction": "work"}) if kind == "coding" else (
        AnalysisSpec, {"question": "count", "inputs": ["12345678-1234-1234-1234-123456789abc"]})
    with pytest.raises(ValueError, match="missing prompt template"):
        model(**fields, prompt_version="missing")
    (alternate_prompts / kind / "prompts/v-test/fix.txt").unlink()
    with pytest.raises(ValueError, match="missing prompt template"):
        model(**fields, prompt_version="v-test")


def test_configured_default_validated_and_pinned(alternate_prompts, monkeypatch):
    monkeypatch.setenv("SCA_DEFAULT_PROMPT_VERSION", "v-test")
    assert CodingSpec(repo_url="x", instruction="work").prompt_version == "v-test"


async def test_ci_workflow_passes_selected_version(monkeypatch):
    from unittest.mock import AsyncMock

    from sca.kinds.coding.activities import CiOutcome
    from sca.kinds.coding.workflow import CodingAfterVerify

    extension = CodingAfterVerify()
    extension._run_ci = AsyncMock(return_value=CiOutcome("task", 1, "sha", False, "failed"))
    execute = AsyncMock(return_value="fix")
    monkeypatch.setattr("sca.kinds.coding.workflow.workflow.execute_activity", execute)
    assert await extension.run("task", {"ci": {}, "prompt_version": "v-test"}, 1) == {
        "passed": False, "prompt": "fix"}
    assert execute.call_args.kwargs["args"] == ["failed", None, "v-test"]


async def test_subtask_workflow_passes_parent_version(monkeypatch):
    from sca.kinds.coding.workflow import CodingSubtaskWorkflow

    calls = []

    async def execute(name, arg=None, **kwargs):
        calls.append((name, arg, kwargs))
        if name == "coding.subtask_prompt":
            return "child prompt"
        assert name == "coding.subtask_step"
        if arg.action == "run_agent_turn":
            return {"session_id": "session"}
        if arg.action == "checkpoint_workspace":
            return {"commit": "sha", "changed": True}
        if arg.action == "run_verification":
            return {"passed": True}
        return {}

    monkeypatch.setattr("sca.kinds.coding.workflow.workflow.execute_activity", execute)
    spec = {"instruction": "parent", "turn_timeout_s": 30, "max_fix_attempts": 0,
            "prompt_version": "v-test"}
    subtask = {"name": "one", "instruction": "child"}
    result = await CodingSubtaskWorkflow().run("task", subtask, spec)
    assert result.passed
    assert calls[0][2]["args"] == [spec, subtask]


async def test_children_inherit_pinned_default(alternate_prompts, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sca.core.models import Task
    from sca.kinds.coding.activities import CodingActivities
    from sca.kinds.coding.subtasks import ChildStore

    task = Task.model_construct(id="12345678-1234-1234-1234-123456789abc", tenant_id="test", kind="coding", spec={"repo_url": "x", "instruction": "work"},
                result={"prompt_version": "v-test", "subtask.one.branch": "child"})
    scoped = ChildStore(SimpleNamespace(get_task=AsyncMock(return_value=task)), "one")
    monkeypatch.setenv("SCA_DEFAULT_PROMPT_VERSION", "v1")
    child_task = await scoped.get_task(str(task.id))
    assert child_task.result == {"prompt_version": "v-test", "branch": "child"}
    acts = SimpleNamespace(store=scoped)
    assert (await CodingActivities._spec(acts, str(task.id))).prompt_version == "v-test"


def test_planning_preserves_reviewer_whitespace():
    expected = Template(OLD["coding/planning"]).substitute(
        instruction=" work\n", reviewer_feedback="\nReviewer feedback:\n change\n\n")
    assert coding_prompts.planning_prompt(" work\n", " change\n", "v1") == expected


async def test_feedback_unmerged_v1_exact():
    rows = [{"name": "one", "status": "conflict"}]
    text = await coding.feedback_prompt(" change \n", rows, "v1")
    expected = Template(OLD["coding/feedback"]).substitute(feedback=(
        "change \n\nUnmerged subtasks; resolve their patches:\n"
        "- one (conflict): /workspace/.sca/subtask-one.patch"))
    assert text == expected
