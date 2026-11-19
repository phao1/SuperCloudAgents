"""Compilers treat input as data; tools only execute inside the sandbox."""

import json
import shlex
from unittest.mock import AsyncMock

import pytest

from sca.intents.base import MAX_SOURCE_BYTES, IntentSource, read_local
from sca.intents.compiler import compile_sources, write_context
from sca.intents.openapi import DEFAULT_CHECKS, OpenAPIAdapter
from sca.intents.rules import RulesAdapter
from sca.intents.visual import VisualAdapter
from sca.kernel.spec import CommonSpec

HOSTILE = "'\"\n$(touch /tmp/pwned)`id`"


@pytest.mark.parametrize("tool", ["ruff", "eslint", "semgrep"])
def test_rules(tmp_path, tool):
    (tmp_path / "rules").write_text(HOSTILE)
    source = IntentSource(
        type="rules", name="lint", path="rules", params={"tool": tool, "description": HOSTILE}
    )
    bundle = RulesAdapter().compile(source, tmp_path)
    command = shlex.split(bundle.checks[0].command)
    if tool == "ruff":
        assert command[:2] == ["python", "-m"]
        command = command[2:]
    assert command[0] == tool
    assert command[-1] == "."
    assert len(command) == (4 if tool == "eslint" else 5)
    assert list(bundle.context_files.values()) == [HOSTILE.encode(), HOSTILE.encode()]
    assert len(bundle.setup) == 1
    assert tool in shlex.split(bundle.setup[0])


def test_openapi_hostile(tmp_path):
    raw = json.dumps({"openapi": "3.0.3", "info": {"title": HOSTILE}, "paths": {}}).encode()
    (tmp_path / "api").write_bytes(raw)
    source = IntentSource(
        type="openapi",
        name="api",
        path="api",
        params={
            "start_command": HOSTILE,
            "base_url": "http://localhost/" + HOSTILE,
            "ready_url": "http://localhost/ready",
        },
    )
    bundle = OpenAPIAdapter().compile(source, tmp_path)
    assert bundle.context_files["api.yaml"] == raw
    assert shlex.split(bundle.checks[0].command) == [
        "python",
        ".sca/context/service_runner.py",
        ".sca/context/api-service.json",
    ]
    config = json.loads(bundle.context_files["api-service.json"])
    assert config["start_command"] == HOSTILE
    assert config["argv"] == [
        "schemathesis",
        "run",
        ".sca/context/api.yaml",
        "--url",
        "http://localhost/" + HOSTILE,
        "--checks",
        ",".join(DEFAULT_CHECKS),
    ]


def test_visual(tmp_path):
    (tmp_path / "base.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    source = IntentSource(
        type="visual",
        name="page",
        path="base.png",
        params={
            "start_command": HOSTILE,
            "base_url": "http://localhost",
            "page_url": "http://localhost/" + HOSTILE,
        },
    )
    bundle = VisualAdapter().compile(source, tmp_path)
    assert shlex.split(bundle.checks[0].command) == [
        "python",
        ".sca/context/service_runner.py",
        ".sca/context/page-service.json",
    ]
    assert bundle.checks[0].artifacts == [".sca/context/visual-page/diff.png"]
    assert (
        json.dumps("http://localhost/" + HOSTILE) in bundle.context_files["page.spec.cjs"].decode()
    )
    assert b"toHaveScreenshot" in bundle.context_files["page.spec.cjs"]


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "link/secret", "symlink"])
def test_no_path_escape(tmp_path, name):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / "symlink").symlink_to("/etc/passwd")
    with pytest.raises((OSError, ValueError)):
        read_local(tmp_path, name)


def test_size_yaml_and_validation(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="size"):
        read_local(tmp_path, "big")
    (tmp_path / "evil").write_text("!!python/object/apply:os.system ['id']")
    with pytest.raises(Exception, match="constructor"):
        OpenAPIAdapter().compile(
            IntentSource(
                type="openapi",
                name="api",
                path="evil",
                params={"start_command": "true", "base_url": "http://localhost"},
            ),
            tmp_path,
        )
    with pytest.raises(ValueError):
        IntentSource(type="rules", name=HOSTILE, path="x")
    assert CommonSpec().intents == []


async def test_upload_tenant_key_and_context(tmp_path):
    objects = AsyncMock()
    objects.get.return_value = b"[lint]\nselect = ['F']"
    source = IntentSource(type="rules", name="lint", file_id="abc-123", params={"tool": "ruff"})
    bundle = await compile_sources([source], tmp_path, objects, "tenant-a")
    objects.get.assert_awaited_once_with("tenants/tenant-a/files/abc-123")
    write_context(tmp_path, bundle)
    assert (tmp_path / ".sca/context/lint.toml").read_bytes() == objects.get.return_value
    with pytest.raises(ValueError, match="unique"):
        await compile_sources([source, source], tmp_path, objects, "tenant-a")


def test_context_refuses_symlink_and_preserves_hardlink(tmp_path):
    from sca.intents.base import IntentBundle

    bundle = IntentBundle(context_files={"rules.toml": b"new"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".sca").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_context(tmp_path, bundle)
    (tmp_path / ".sca").unlink()
    write_context(tmp_path, bundle)
    original = tmp_path / "original"
    original.write_bytes(b"original")
    context = tmp_path / ".sca/context/rules.toml"
    context.unlink()
    context.hardlink_to(original)
    write_context(tmp_path, bundle)
    assert original.read_bytes() == b"original"
    assert context.read_bytes() == b"new"


def test_openapi_checks_override_and_validation(tmp_path):
    import pytest
    from pydantic import ValidationError

    from sca.intents.openapi import ServiceParams

    params = ServiceParams(start_command="x", base_url="http://localhost", checks=["all"])
    assert params.checks == ["all"]
    with pytest.raises(ValidationError):
        ServiceParams(start_command="x", base_url="http://localhost", checks=["all; rm -rf /"])
