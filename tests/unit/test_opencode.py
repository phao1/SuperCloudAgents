import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_opencode_fixture import TRANSCRIPT

from sca.agents.opencode import OpenCodeBackend, parse_opencode_line, validate_version
from sca.agents.opencode_build import build_command, perform_build, reserve_build
from sca.api.agents import FixRequest, fix_spec
from sca.config import HOST_ARCH, Settings
from sca.core.errors import AgentTurnError
from sca.core.models import AgentContext, Event, ExecExit, ExecOutput, Task
from sca.kinds.coding.spec import CodingSpec


def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path)


def test_transcript():
    events = [e for line in TRANSCRIPT.splitlines() for e in parse_opencode_line(line)]
    command = next(e for e in events if e.type == "command")
    assert command.data["exit_code"] == 0
    patch = next(e for e in events if e.type == "file_change")
    assert "-    return sum(xs) / len(xs) + 1" in patch.data["changes"][0]["patch"]
    assert sum(e.data["cached_input_tokens"] for e in events if e.type == "usage") == 23936
    assert any(e.type == "tool" and e.data["name"] == "read" for e in events)


@pytest.mark.parametrize(
    "version", ["../foo", "2.3.0-sca.01", "2.3.0-sca.-1", "x", "2.3.0-sca.1\n"]
)
def test_bad_version(version):
    with pytest.raises(ValueError):
        validate_version(version)
    with pytest.raises(ValidationError):
        CodingSpec(repo_url="r", instruction="i", agent_version=version)


def backend(tmp_path):
    version = tmp_path / "2.3.0-sca.1"
    version.mkdir()
    (version / "opencode").touch()
    return OpenCodeBackend(tmp_path, version.name, "model")


def test_selection(tmp_path):
    agent = backend(tmp_path)
    ctx = AgentContext(task_id="id", agent_home_host=str(tmp_path / "home"))
    mounts = agent.sandbox_additions(ctx).mounts
    assert mounts[0].source == str(tmp_path / "2.3.0-sca.1")
    assert mounts[0].read_only
    with pytest.raises(AgentTurnError, match="Unknown"):
        agent.sandbox_additions(ctx.model_copy(update={"agent_version": "2.3.0-sca.99"}))


@pytest.mark.parametrize("code,complete", [(0, True), (1, True), (0, False)])
async def test_turn(tmp_path, code, complete):
    agent = backend(tmp_path)
    ctx = AgentContext(
        task_id="id",
        agent_home_host=str(tmp_path),
        model_base_url="http://gateway/v1",
        model_api_key="temporary",
    )

    class Sandbox:
        async def exec(self, sid, argv, **kwargs):
            assert kwargs["env"]["SCA_MODEL_KEY"] == "temporary"
            assert "temporary" not in " ".join(argv)
            assert argv[-1] == "prompt"
            for line in TRANSCRIPT.splitlines():
                if not complete and '"reason":"stop"' in line:
                    continue
                yield ExecOutput(stream="stdout", text=line)
            yield ExecExit(exit_code=code)

    async def collect(context):
        return [
            e
            async for e in agent.run_turn(
                Sandbox(), "sandbox", context, "prompt", session_id=None, timeout_s=30
            )
        ]

    if code or not complete:
        with pytest.raises(AgentTurnError):
            await collect(ctx)
    else:
        events = await collect(ctx)
        assert events[0].type == "session_started"
        assert events[-1].type == "turn_completed"
    with pytest.raises(AgentTurnError, match="requires"):
        await collect(ctx.model_copy(update={"model_api_key": None}))


async def test_build(tmp_path):
    cfg = settings(tmp_path)
    cache = cfg.opencode_src_cache / "node_modules"
    cache.mkdir(parents=True)
    (cache / "marker").write_text("cached")
    record = reserve_build(cfg, str(uuid4()), "tenant", ref="main", notes="test",
                           labels={"kind": "opencode-fix"})
    assert record["version"] == "2.3.0-sca.1"
    assert reserve_build(cfg, str(uuid4()), "tenant", ref="main")["version"] == "2.3.0-sca.2"
    calls = []

    async def runner(argv, log):
        calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir()
        if "rev-parse" in argv:
            return "a" * 40
        if argv[0] == "docker":
            source = Path(argv[argv.index("-v") + 1].removesuffix(":/src"))
            assert not (source / "node_modules/marker").exists()
            assert f"{cache}:/src/node_modules:ro" in argv
            binary = source / f"packages/cli/dist/cli-linux-{HOST_ARCH}/bin/opencode"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fake executable")
        return ""

    result = await perform_build(cfg, record, runner)
    assert result["status"] == "succeeded"
    assert not (tmp_path / "opencode/build-work" / record["id"]).exists()
    meta = json.loads((cfg.opencode_versions_dir / record["version"] / "meta.json").read_text())
    assert meta["commit"] == "a" * 40
    assert meta["labels"] == {"kind": "opencode-fix"}
    command = build_command(cfg, tmp_path, record["version"])
    assert "--memory=2500m" in command and "--network=none" in command
    assert not any("install" == arg for call in calls for arg in call)


async def test_build_failure(tmp_path):
    cfg = settings(tmp_path)
    record = reserve_build(cfg, str(uuid4()), "t", ref="main")

    async def fail(argv, log):
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError):
        await perform_build(cfg, record, fail)
    path = cfg.opencode_versions_dir / "builds" / f"{record['id']}.json"
    assert json.loads(path.read_text())["status"] == "failed"
    assert not (cfg.opencode_versions_dir / record["version"] / "meta.json").exists()


def make_task():
    return Task(
        id=uuid4(),
        tenant_id="t",
        kind="coding",
        state="FAILED",
        spec={"instruction": "preserve Chinese", "eval_run_id": "run"},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_fix(tmp_path):
    task = make_task()
    event = Event(
        task_id=task.id,
        seq=1,
        type="agent.file_change",
        payload={"patch": "damage"},
        created_at=task.created_at,
    )
    spec = fix_spec(
        settings(tmp_path),
        task,
        [event],
        FixRequest(failing_task_id=task.id, agent_backend="opencode"),
    )
    assert spec.push and spec.image == "sca-sandbox-opencode:latest"
    assert "damage" in spec.instruction and "preserve Chinese" in spec.instruction
    assert "bun test" in spec.verify_command
    assert "install" not in spec.setup_command


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"part": []},
        {"type": "tool_use", "part": {"state": {"input": [], "metadata": {"metadata": []}}}},
        {"type": "step_finish", "part": {"tokens": []}},
        {"type": "error", "error": "failed"},
    ],
)
def test_malformed_events(raw):
    parse_opencode_line(json.dumps(raw))


async def test_build_serialization(tmp_path):
    import fcntl

    cfg = settings(tmp_path)
    record = reserve_build(cfg, str(uuid4()), "t", ref="main")
    with (cfg.opencode_versions_dir / "builds/build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another"):
            await perform_build(cfg, record)


async def test_build_workflow_sandbox():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    from sca.agents.opencode_build import OpenCodeBuildWorkflow

    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(OpenCodeBuildWorkflow)
    )


def test_demo_acceptance(tmp_path):
    import subprocess
    import sys

    from sca.eval.suites import load_suite

    suite = load_suite(Path("evals/opencode-demo"))
    assert len(suite["cases"]) == 6
    assert suite["defaults"] == {"max_fix_attempts": 1, "turn_timeout_s": 300}
    solutions = {
        "bug-clamp": "def clamp(value, low, high): return max(low, min(high, value))",
        "bug-mean": "def mean(values):\n    if not values: raise ValueError()\n    return sum(values)/len(values)",
        "bug-unique": "def unique(values):\n    result=[]\n    for value in values:\n        if value not in result: result.append(value)\n    return result",
        "feature-merge": "def merge_counts(*mappings):\n    result={}\n    for mapping in mappings:\n        for key,value in mapping.items(): result[key]=result.get(key,0)+value\n    return result",
        "feature-chunks": "def chunks(values,size):\n    if size<=0: raise ValueError()\n    return [list(values[i:i+size]) for i in range(0,len(values),size)]",
    }
    escaped = (
        'GREETING = "\\u4f60\\u597d\\uff0c\\u4e16\\u754c"\n'
        'FAREWELL = "\\u4e0b\\u6b21\\u518d\\u89c1"\n\ndef add(a, b):\n    return a + b\n'
    )
    failures = []
    for case in suite["cases"]:
        root = Path(case["path"])
        code = solutions.get(case["id"])
        if code is None:
            code = (root / "repo/app.py").read_text().replace("return a - b", "return a + b")
        variants = {"intact": code,
                    "damaged": "".join(c for c in code if not ("\u2e80" <= c <= "\u9fff"))}
        if case["id"] == "chinese-greeting":
            variants["escaped"] = escaped  # the workaround a model found: not "kept as is"
        for name, text in variants.items():
            work = tmp_path / case["id"] / name
            work.mkdir(parents=True)
            (work / "app.py").write_text(text, encoding="utf-8")
            hidden = (root / "acceptance/hidden.py").read_text(encoding="utf-8")
            (work / "hidden.py").write_text(hidden, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "hidden.py"], cwd=work, capture_output=True, check=False
            )
            if result.returncode:
                assert name != "intact", result.stderr.decode()
                failures.append(f"{case['id']}:{name}")
    assert failures == ["chinese-greeting:damaged", "chinese-greeting:escaped"]


async def test_setup_dry_run(tmp_path, capsys):
    import runpy

    cfg = settings(tmp_path)
    cfg.opencode_src_cache.mkdir(parents=True)
    Path(cfg.opencode_repo_url).mkdir()
    namespace = runpy.run_path("scripts/opencode_setup.py")
    setup = namespace["setup"]
    setup.__globals__["get_settings"] = lambda: cfg

    async def unexpected(*args):
        pytest.fail("Existing repositories must not be recreated")

    setup.__globals__["run_command"] = unexpected
    await setup(False)
    await setup(False)
    output = capsys.readouterr().out
    assert "docker build" in output
    assert "--memory=2500m" in output
    assert not (tmp_path / "opencode/image-context").exists()


async def test_build_rejects_checkout_symlink(tmp_path):
    cfg = settings(tmp_path)
    (cfg.opencode_src_cache / "node_modules").mkdir(parents=True)
    (cfg.opencode_src_cache / "packages/core/node_modules").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    record = reserve_build(cfg, str(uuid4()), "t", ref="main")

    async def runner(argv, log):
        if argv[:2] == ["git", "clone"]:
            source = Path(argv[-1])
            source.mkdir()
            (source / "packages").symlink_to(outside, target_is_directory=True)
        return "a" * 40

    with pytest.raises(RuntimeError, match="escapes"):
        await perform_build(cfg, record, runner)
    assert list(outside.iterdir()) == []


def test_trace_timing():
    steps = {}
    events = [e for line in TRANSCRIPT.splitlines() for e in parse_opencode_line(line, steps)]
    command = next(e for e in events if e.type == "command")
    assert command.data["started_at_ms"] == 1794830322321
    assert command.data["ended_at_ms"] == 1794830322565
    assert command.data["duration_ms"] == 244
    for event in events:
        if event.type in {"command", "tool", "file_change"}:
            assert event.data["duration_ms"] == (
                event.data["ended_at_ms"] - event.data["started_at_ms"]
            )
    usage = next(e for e in events if e.type == "usage")
    assert usage.data["started_at_ms"] == 1794830314516
    assert usage.data["ended_at_ms"] == 1794830322580
    assert usage.data["model_call_duration_ms"] == 7805
    assert steps == {}


def test_trace_timing_without_tools():
    steps = {}
    parse_opencode_line(json.dumps({"type": "step_start", "timestamp": 100}), steps)
    event = parse_opencode_line(json.dumps({"type": "step_finish", "timestamp": 150}), steps)[0]
    assert event.data["model_call_duration_ms"] == 50
    assert event.data["duration_ms"] == 50


@pytest.mark.parametrize('timing', [None, [], {'start': True, 'end': 50},
                                    {'start': 100, 'end': 50}, {'start': '10', 'end': 50}])
def test_invalid_tool_timing(timing):
    event = parse_opencode_line(json.dumps({
        'type': 'tool_use', 'part': {'tool': 'read', 'state': {'time': timing}}
    }))[0]
    assert 'duration_ms' not in event.data


@pytest.mark.parametrize("labels", [
    {"bad/key": "v"}, {"": "v"}, {"x" * 65: "v"}, {"key": "v" * 257},
    {str(i): "v" for i in range(17)}, {"key": 12},
])
def test_label_limits(labels):
    with pytest.raises(ValidationError):
        CodingSpec(repo_url="r", instruction="i", labels=labels)


def test_fix_labels(tmp_path):
    task = make_task()
    task.spec.update(eval_case_id="unicode", agent_version="2.3.0-sca.3")
    spec = fix_spec(settings(tmp_path), task, [],
                    FixRequest(failing_task_id=task.id, agent_backend="codex"))
    assert spec.labels == {
        "kind": "opencode-fix", "source_task_id": str(task.id), "eval_run_id": "run",
        "case_id": "unicode", "source_version": "2.3.0-sca.3",
    }
    assert CodingSpec.model_validate(spec.model_dump()).labels == spec.labels


async def test_live_build_output(tmp_path):
    import asyncio
    import sys

    from sca.agents.opencode_build import run_command

    log = tmp_path / "build.log"
    release = tmp_path / "release"
    code = ("import pathlib,time; print('progress', flush=True); "
            f"p=pathlib.Path({str(release)!r}); "
            "exec('while not p.exists(): time.sleep(0.01)'); print('done', flush=True)")
    task = asyncio.create_task(run_command([sys.executable, "-c", code], log))
    try:
        async with asyncio.timeout(5):
            while not log.exists() or b"progress" not in log.read_bytes():
                await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.touch()
        await task
    assert log.read_text() == "progress\ndone\n"


def test_fix_dependency_mounts(tmp_path):
    from sca.core.models import SandboxAdditions
    from sca.kinds.coding.activities import DependencyBackend

    cfg = settings(tmp_path)
    root = cfg.opencode_src_cache
    (root / "node_modules/@opencode").mkdir(parents=True)
    (root / "node_modules/@opencode/core").symlink_to("../../packages/core")
    (root / "packages/core/node_modules").mkdir(parents=True)

    class Backend:
        def sandbox_additions(self, ctx):
            return SandboxAdditions()

    additions = DependencyBackend(Backend(), cfg).sandbox_additions(None)
    assert [(m.target, m.read_only) for m in additions.mounts] == [
        ("/workspace/node_modules", True), ("/workspace/packages/core/node_modules", True)
    ]
    assert (root / "node_modules/@opencode/core").readlink() == Path("../../packages/core")
    command = build_command(cfg, tmp_path / "checkout", "2.3.0-sca.1")
    assert f"{root}/packages/core/node_modules:/src/packages/core/node_modules:ro" in command


async def test_kernel_sandbox_uses_the_kinds_backend(tmp_path):
    """The kernel instance runs create_sandbox; the kind's backend wrapper must still apply.

    Regression: OpenCode fix sandboxes were created without the dependency mounts because
    the kernel asked its own (unwrapped) backend for the sandbox additions.
    """
    from types import SimpleNamespace

    from sca.core.models import Mount, SandboxAdditions
    from sca.kernel.activities import KernelActivities

    class Plain:
        sandbox_network = "restricted"

        def sandbox_additions(self, ctx):
            return SandboxAdditions()

    class Wrapped(Plain):
        sandbox_network = "none"

        def sandbox_additions(self, ctx):
            return SandboxAdditions(mounts=[Mount(source="/deps", target="/workspace/deps")])

    created = []
    task = SimpleNamespace(kind="coding", tenant_id="t", result={}, spec={})

    async def get_task(task_id):
        return task

    async def noop(*args, **kwargs):
        return None

    async def create(name, spec):
        created.append(spec)
        return "sandbox"

    def fake(backend):
        acts = object.__new__(KernelActivities)
        acts.settings = settings(tmp_path)
        acts.store = SimpleNamespace(get_task=get_task, update_result=noop)
        acts.transport = SimpleNamespace(mounts=lambda task_dir: [], push=noop)
        acts.sandbox = SimpleNamespace(create=create)
        acts._ensure_state = noop
        acts._spec = lambda task_id: noop_spec()
        acts._backend = lambda spec: backend
        return acts

    async def noop_spec():
        return SimpleNamespace(image="img", model="m", agent_version=None)

    kernel, scenario = fake(Plain()), fake(Wrapped())
    kernel.scenarios = {"coding": scenario}
    await kernel._create_sandbox(str(uuid4()))
    assert "/workspace/deps" in [m.target for m in created[0].mounts]
    assert created[0].network == "none"
