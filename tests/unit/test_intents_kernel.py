"""Kernel compilation and diff feedback without external services."""

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sca.core.models import ExecExit
from sca.kernel.activities import KernelActivities, StepInput


def activities(tmp_path):
    kernel = object.__new__(KernelActivities)
    kernel.store = AsyncMock()
    kernel.objects = AsyncMock()
    kernel._workspace = Mock(return_value=SimpleNamespace(workspace=tmp_path))
    kernel._sandbox_id = AsyncMock(return_value="sandbox")
    kernel._publish_artifact = AsyncMock()
    return kernel


async def test_compile_context_and_event(tmp_path):
    (tmp_path / "ruff.toml").write_text('[lint]\nselect = ["F"]')
    kernel = activities(tmp_path)
    kernel.store.get_task.return_value = SimpleNamespace(
        tenant_id="t",
        result={},
        spec={
            "intents": [
                {"name": "lint", "type": "rules", "path": "ruff.toml", "params": {"tool": "ruff"}}
            ]
        },
    )
    result = await kernel.compile_intents("task")
    assert result["checks"][0]["name"] == "lint"
    assert ".sca/context/lint.toml" in result["notes"][0]
    assert (tmp_path / ".sca/context/lint.toml").is_file()
    event = kernel.store.append_event.call_args
    assert event.args[1] == "intents.compiled"
    assert event.kwargs["dedup_key"] == "intents.compiled"
    assert event.args[2]["sources"][0]["checks"] == ["lint"]
    kernel.objects.put.assert_awaited_once()


async def test_diff_artifact_feedback(tmp_path):
    kernel = activities(tmp_path)
    path = ".sca/context/visual-page/diff.png"
    kernel.store.get_task.return_value = SimpleNamespace(
        result={"intents": {"checks": [{"name": "page", "command": "false", "artifacts": [path]}]}}
    )
    kernel._run = AsyncMock(return_value=(ExecExit(exit_code=1), "comparison failed"))
    data = b"\x89PNG\r\n\x1a\nimage"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        entry = tarfile.TarInfo("diff.png")
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))

    async def download(*args):
        yield archive.getvalue()

    kernel.sandbox = SimpleNamespace(download=download)
    result = await kernel.verify_intents(StepInput("task", 1))
    assert not result.passed
    assert path in result.output_tail
    assert "intent-page-turn1.png" in result.output_tail
    kernel._publish_artifact.assert_awaited_once_with(
        "task", "intent-page-turn1.png", data, "image/png"
    )
