import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from sca import runtime_switch
from sca.runtime_mode import preferred_mode
from scripts import portal_switch


@pytest.fixture
def switch(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_switch, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.delenv("SCA_KEEP_SWITCH_ARCHIVE", raising=False)
    monkeypatch.setenv("SCA_DATABASE_URL", "postgresql+asyncpg://source.invalid/sca")
    monkeypatch.setenv("SCA_SANDBOX_DRIVER", "local")
    monkeypatch.setenv("SCA_OBJECT_STORE", "local")

    def setup(source="source", target="docker", *, carry_history=True, fail_first=None):
        root = tmp_path / "checkout with spaces"
        data = root / ".data"
        record = runtime_switch.SwitchRecord(id=uuid4(), source=source, target=target, carry_history=carry_history)
        runtime_switch.save_preference(data, target)
        record.save(data)
        archive = data / "switch" / f"{record.id}.zip"
        calls = []

        def fail_step(step):
            assert runtime_switch.read_switch(data).step == step
            if step == fail_first:
                raise subprocess.CalledProcessError(7, [step], stderr="fixture-only private output")

        def launch(argv, *, cwd, env, check, stdin, stdout, stderr):
            mode = env["SCA_RUNTIME_MODE"]
            assert cwd == root and check and stdin == stdout == stderr == subprocess.DEVNULL
            assert argv[:2] == ["bash", str(root / "scripts/portal_demo.sh")]
            command, *args = argv[2:]
            calls.append((mode, command))
            if command == "down":
                assert args == (["--all"] if mode == "docker" else [])
            else:
                assert args[:2] == ["--mode", mode]
            assert env["SCA_OBJECT_STORE"] == "local"
            if source == "source" and mode == "docker":
                assert "SCA_DATABASE_URL" not in env and "SCA_SANDBOX_DRIVER" not in env
            elif source == "source":
                assert env["SCA_DATABASE_URL"] == "postgresql+asyncpg://source.invalid/sca"
                assert env["SCA_SANDBOX_DRIVER"] == "local"
            step = {"export-history": "export", "down": "stop", "up": "start", "import-history": "import"}[command]
            if record.step != "rollback":
                fail_step(step)
            if command == "export-history":
                assert Path(args[-1]) == archive
                archive.write_bytes(b"fixture archive")
            if command == "import-history":
                assert Path(args[-1]) == archive
                assert archive.read_bytes() == b"fixture archive"

        def health():
            calls.append((target, "healthz"))
            fail_step("health")

        monkeypatch.setattr(portal_switch.subprocess, "run", launch)
        monkeypatch.setattr(portal_switch, "wait_for_health", health)
        return SimpleNamespace(root=root, data=data, record=record, archive=archive, calls=calls)

    return setup


@pytest.mark.parametrize(("source", "target", "keep"), [("source", "docker", "0"), ("docker", "source", "1")])
def test_switch_runs_steps_in_order_and_handles_zip(switch, monkeypatch, source, target, keep):
    monkeypatch.setenv("SCA_KEEP_SWITCH_ARCHIVE", keep)
    job = switch(source, target)
    assert portal_switch.run_switch(job.root, job.record)
    assert job.calls == [
        (source, "export-history"), (source, "down"), (target, "up"), (target, "import-history"), (target, "healthz"),
    ]
    record = runtime_switch.read_switch(job.data)
    assert record.status == "succeeded" and record.step == "done" and record.error is None
    assert record.updated_at >= record.started_at and record.log_tail.endswith("切换完成")
    assert preferred_mode(job.data) == target
    assert job.archive.exists() == (keep == "1")


@pytest.mark.parametrize(("failure", "commands"), [
    ("export", [("source", "export-history"), ("source", "up")]),
    ("start", [("source", "export-history"), ("source", "down"), ("docker", "up"),
               ("docker", "down"), ("source", "up")]),
    ("import", [("source", "export-history"), ("source", "down"), ("docker", "up"),
                ("docker", "import-history"), ("docker", "down"), ("source", "up")]),
    ("health", [("source", "export-history"), ("source", "down"), ("docker", "up"),
                ("docker", "import-history"), ("docker", "healthz"), ("docker", "down"), ("source", "up")]),
])
def test_switch_failure_restores_original_mode_and_keeps_export(switch, failure, commands):
    job = switch(fail_first=failure)
    assert not portal_switch.run_switch(job.root, job.record)
    assert job.calls == commands
    assert preferred_mode(job.data) == "source"
    record = runtime_switch.read_switch(job.data)
    assert record.status == "failed" and record.step == failure
    assert "恢复原运行模式" in record.error and "退出码 7" in record.error
    assert "fixture-only" not in record.model_dump_json()
    assert job.archive.exists() == (failure != "export")


def test_switch_can_explicitly_skip_history(switch):
    job = switch(carry_history=False)
    assert portal_switch.run_switch(job.root, job.record)
    assert job.calls == [("source", "down"), ("docker", "up"), ("docker", "healthz")]
    assert not job.archive.parent.exists()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_spawn_detaches_the_helper(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(runtime_switch, "sys", SimpleNamespace(platform=platform, executable=sys.executable))
    process = Mock()
    monkeypatch.setattr(runtime_switch.subprocess, "Popen", process)
    options = {"start_new_session": True}
    if platform == "win32":
        flags = {"DETACHED_PROCESS": 8, "CREATE_NEW_PROCESS_GROUP": 0x200, "CREATE_BREAKAWAY_FROM_JOB": 0x1000000}
        for name, value in flags.items():
            monkeypatch.setattr(runtime_switch.subprocess, name, value, raising=False)
        options = {"creationflags": sum(flags.values())}
    job_id = uuid4()
    runtime_switch.launch_switch(tmp_path, job_id)
    process.assert_called_once_with(
        [sys.executable, "-X", "utf8", str(tmp_path / "scripts/portal_switch.py"), str(job_id)],
        cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options,
    )
