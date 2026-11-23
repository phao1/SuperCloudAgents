import asyncio
import os
import re
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from sca.api import app as api_app
from sca.api import portal
from sca.config import Settings
from sca.core.errors import InvalidTaskError
from sca.core.models import Task, TaskState
from sca.projects import prepare_thread_spec
from sca.workspace.git import GitWorkspace
from sca.workspace.output import prepare_output, write_output

ORIGIN = "http://127.0.0.1:8000"
ADMIN = {"Authorization": "Bearer local-fixture"}
SUBJECT = "Create Flask shop"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=test@localhost",
         "-c", "commit.gpgsign=false", "-C", path.as_posix(), *args],
        capture_output=True, check=True, encoding="utf-8", timeout=30,
    ).stdout.strip()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data", projects_dir=tmp_path / "projects")


@pytest.mark.parametrize("exists", [False, True])
def test_prepare_empty_output(tmp_path, settings, exists):
    path = tmp_path / "shop with spaces"
    if exists:
        path.mkdir()
    raw = f"{tmp_path.as_posix()}/nested/../{path.name}"
    output, branch, target = prepare_output(settings, raw, SUBJECT, None)
    assert (output, branch) == (path.as_posix(), "main")
    assert target.branch == branch
    assert git(path, "rev-parse", "HEAD") == target.head == target.base
    assert git(path, "log", "-1", "--format=%s") == "Initial empty commit"
    assert git(path, "ls-files") == ""
    moved = path.rename(tmp_path / "moved")
    assert git(moved, "rev-parse", "--show-toplevel") == moved.as_posix()


def test_prepare_imports_existing_files_and_reuses_the_current_branch(tmp_path, settings):
    path = tmp_path / "shop"
    path.mkdir()
    (path / "README.md").write_text("Existing code", encoding="utf-8")
    _, _, target = prepare_output(settings, str(path), SUBJECT, None)
    assert git(path, "log", "-1", "--format=%s") == "Import existing files"
    assert git(path, "show", "HEAD:README.md") == "Existing code"
    git(path, "tag", "first-import")
    git(path, "switch", "-c", "dev")
    git(path, "commit", "--allow-empty", "-m", "New base")
    _, branch, reused = prepare_output(settings, str(path), SUBJECT, None)
    assert branch == reused.branch == "dev"
    assert reused.head == reused.base == git(path, "rev-parse", "HEAD") != target.head
    _, base_ref, tagged = prepare_output(settings, str(path), SUBJECT, "first-import")
    assert base_ref == "first-import"
    assert tagged.branch == "dev" and tagged.head == reused.head and tagged.base == target.head
    with pytest.raises(InvalidTaskError, match="基线引用不存在"):
        prepare_output(settings, str(path), SUBJECT, "missing-branch")


@pytest.mark.parametrize("change", ["untracked", "staged", "modified", "detached", "bare", "file"])
def test_prepare_refuses_an_unusable_checkout(tmp_path, settings, change):
    path = tmp_path / "shop"
    if change == "file":
        path.touch()
        error = "无法准备输出目录"
    else:
        path.mkdir()
        if change == "bare":
            git(path, "init", "--bare")
            error = "工作区"
        else:
            (path / "README.md").write_text("Baseline", encoding="utf-8")
            prepare_output(settings, str(path), SUBJECT, None)
            error = "有未提交的更改"
            if change == "detached":
                git(path, "switch", "--detach")
                error = "检出一个 Git 分支"
            elif change == "modified":
                (path / "README.md").write_text("User edit", encoding="utf-8")
            else:
                (path / "notes").touch()
                if change == "staged":
                    git(path, "add", "notes")
    with pytest.raises(InvalidTaskError, match=error):
        prepare_output(settings, str(path), SUBJECT, None)


def test_default_projects_root_and_slug(settings, monkeypatch):
    monkeypatch.delenv("SCA_PROJECTS_DIR", raising=False)
    assert Settings(_env_file=None).projects_root == (Path.home() / "sca-projects").resolve()
    monkeypatch.setenv("SCA_PROJECTS_DIR", str(settings.projects_dir))
    assert Settings(_env_file=None).projects_root == settings.projects_root
    first, _, _ = prepare_output(settings, None, "Flask shop!\nMore details", None)
    second, _, _ = prepare_output(settings, None, "Flask shop!\nMore details", None)
    assert first != second
    for output in (first, second):
        path = Path(output)
        assert path.parent == settings.projects_root
        assert re.fullmatch(r"flask-shop-[a-f0-9]{6}", path.name)
        assert git(path, "branch", "--show-current") == "main"


def test_protects_system_home_platform_and_storage(tmp_path, settings):
    root = Path(__file__).resolve().parents[2]
    for invalid in ("relative", "~/shop", "shop\0bad", Path(root.anchor), Path.home(),
                    root, root / ".git" / "objects", root / ".data" / "objects",
                    settings.data_dir, settings.data_dir / "tasks" / "project"):
        with pytest.raises(InvalidTaskError, match="输出目录"):
            prepare_output(settings, str(invalid), SUBJECT, None)
    alias = tmp_path / "system-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(InvalidTaskError, match="SCA 仓库"):
            prepare_output(settings, str(alias), SUBJECT, None)
    allowed = settings.model_copy(update={"projects_dir": settings.data_dir / "projects"})
    created, _, _ = prepare_output(allowed, None, SUBJECT, None)
    assert Path(created).parent == allowed.projects_root


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows drive paths")
def test_windows_drive_uses_either_separator(tmp_path, settings):
    path = tmp_path / "flask shop"
    output, _, _ = prepare_output(settings, str(path), SUBJECT, None)
    assert output == path.as_posix()
    assert prepare_output(settings, path.as_posix(), SUBJECT, None)[0] == output
    with pytest.raises(InvalidTaskError, match="本机绝对路径"):
        prepare_output(settings, f"{path.drive}relative", SUBJECT, None)


@pytest.fixture
def make_api(settings, monkeypatch):
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("SCA_RUNTIME_MODE", "docker")
    settings.api_tokens = "local-fixture:eval:admin,member-fixture:eval:member,team-fixture:team:admin"
    settings.eval_api_token = SecretStr("local-fixture")
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr(portal, "get_settings", lambda: settings)
    monkeypatch.setattr(portal, "gateway_models", AsyncMock(return_value=(False, [])))

    def make(*, peer="127.0.0.1", url=ORIGIN, port=8000, repos=()):
        rows = {}

        async def create_task(*, tenant_id, kind, spec, validate_spec, initial_result,
                              idempotency_key, **kwargs):
            if idempotency_key is not None and idempotency_key in rows:
                return rows[idempotency_key], False
            project = {"repos": repos, "default_backend": None, "default_model": None,
                       "instructions": ""}
            now = datetime.now(UTC)
            task = Task(
                id=uuid4(), tenant_id=tenant_id, kind=kind,
                spec=prepare_thread_spec(kind, spec, project, validate_spec),
                result=initial_result, state=TaskState.QUEUED, created_at=now, updated_at=now,
            )
            rows[idempotency_key] = task
            return task, True

        store = SimpleNamespace(
            sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(url=make_url(settings.database_url))}),
            tenant_deleted=AsyncMock(return_value=False),
            create_task=create_task,
        )
        store.for_tenant = lambda _: store
        orchestrator = SimpleNamespace(start_task=AsyncMock())
        app = api_app.create_app(store=store, orchestrator=orchestrator, local_portal_port=port)
        transport = httpx.ASGITransport(app=app, client=(peer, 32100))
        return httpx.AsyncClient(transport=transport, base_url=url)

    return make


def submit(spec=None):
    return {"kind": "coding", "spec": {"instruction": SUBJECT, **(spec or {})}}


@pytest.mark.parametrize("mode", ["source", "docker"])
@pytest.mark.parametrize("headers", [ADMIN, {"Origin": ORIGIN}])
async def test_local_portal_admits_output_in_both_modes(make_api, settings, monkeypatch, mode, headers):
    monkeypatch.setenv("SCA_RUNTIME_MODE", mode)
    settings.sandbox_driver = "local" if mode == "source" else "docker"
    async with make_api() as client:
        config = await client.get("/v1/portal/config", headers=headers)
        request_headers = {**headers, "Idempotency-Key": "new-shop"}
        response = await client.post("/v1/tasks", json=submit({"agent_backend": "fake"}),
                                     headers=request_headers)
        replay = await client.post("/v1/tasks", json=submit({"agent_backend": "fake"}),
                                   headers=request_headers)
    assert config.json()["projects_dir"] == settings.projects_root.as_posix()
    assert response.status_code == 201, response.text
    result = response.json()
    spec = result["spec"]
    assert spec["output_dir"] == spec["repo_url"]
    assert spec["plan"] == "auto" and spec["plan_review"] is False and spec["auto_finish"] is False
    assert result["result"]["output_target"]["head"] == git(Path(spec["output_dir"]), "rev-parse", "HEAD")
    assert replay.json()["id"] == result["id"]
    assert list(settings.projects_root.iterdir()) == [Path(spec["output_dir"])]


@pytest.mark.parametrize(("connection", "headers"), [
    ({"peer": "192.0.2.12"}, ADMIN), ({"port": None}, ADMIN),
    ({}, {"Authorization": "Bearer team-fixture"}), ({}, {"Authorization": "Bearer member-fixture"}),
    ({}, {**ADMIN, "X-Forwarded-For": "192.0.2.12"}),
    ({}, {**ADMIN, "Origin": "http://other.test"}),
])
async def test_output_gate_rejects_explicit_and_default_paths(make_api, settings, connection, headers):
    output = settings.projects_root / "shop"
    async with make_api(**connection) as client:
        for spec in ({}, {"output_dir": str(output)}):
            response = await client.post("/v1/tasks", json=submit(spec), headers=headers)
            assert response.status_code == 403, response.text
            assert response.json()["error"]["message"] == "仅本机部署支持输出目录"
    assert not settings.projects_root.exists()


async def test_existing_repo_and_project_repo_still_work_remotely(make_api, settings):
    repo = "https://example.test/shop.git"
    other = {"Authorization": "Bearer team-fixture"}
    async with make_api(peer="192.0.2.9") as client:
        response = await client.post("/v1/tasks", json=submit({"repo_url": repo}), headers=other)
        assert response.status_code == 201
        assert "output_dir" not in response.json()["spec"]
    async with make_api(repos=[{"repo_url": repo, "base_ref": "dev"}]) as client:
        inherited = await client.post("/v1/tasks", json=submit({"repo_url": " "}), headers=other)
        assert inherited.json()["spec"]["repo_url"] == repo
        assert inherited.json()["spec"]["base_ref"] == "dev"
        invalid = await client.post("/v1/tasks", json=submit({"output_dir": str(settings.projects_dir)}),
                                    headers=ADMIN)
        assert invalid.status_code == 422
    assert not settings.projects_root.exists()


@pytest.fixture
async def prepared_result(tmp_path, settings):
    output = tmp_path / "shop"
    output.mkdir()
    (output / "README.md").write_text("Baseline", encoding="utf-8")
    (output / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    path, _, target = prepare_output(settings, str(output), SUBJECT + "\nNext line", None)
    ws = GitWorkspace(tmp_path / "task", "sca-fixture")
    await ws.prepare(path, target.base)
    (ws.workspace / "README.md").write_text("Agent result", encoding="utf-8")
    (ws.workspace / "ignored.txt").write_text("Agent ignored result", encoding="utf-8")
    git(ws.workspace, "add", "-f", "README.md", "ignored.txt")
    git(ws.workspace, "commit", "-m", "Agent checkpoint")
    commit = await ws.fetch()
    now = datetime.now(UTC)
    task = Task(
        id=uuid4(), tenant_id="eval", kind="coding", state=TaskState.AWAITING_REVIEW,
        created_at=now, updated_at=now, spec={"output_dir": path, "repo_url": path},
        result={"base_sha": target.base, "commit": commit, "output_target": asdict(target)},
    )
    return output, task, ws.mirror


def test_writeback_fast_forwards_once(prepared_result, settings):
    output, task, mirror = prepared_result
    result = write_output(settings, task, mirror)
    assert result["output_on_side_branch"] is False
    assert result["output_branch"] == "main"
    assert result["output_commit"] == git(output, "rev-parse", "HEAD")
    assert git(output, "rev-parse", "HEAD^") == task.result["base_sha"]
    assert git(output, "log", "-1", "--format=%an%n%s") == "SCA Agent\n" + SUBJECT
    assert (output / "README.md").read_text(encoding="utf-8") == "Agent result"
    assert git(output, "status", "--porcelain") == ""
    assert write_output(settings, task, mirror) == result


@pytest.mark.parametrize("change", ["dirty", "diverged", "ignored-collision", "branch-switched"])
def test_writeback_preserves_user_edits(prepared_result, settings, change):
    output, task, mirror = prepared_result
    user_file = output / ("ignored.txt" if change == "ignored-collision" else "README.md")
    if change == "branch-switched":
        git(output, "switch", "-c", "user-work")
    else:
        user_file.write_text("User work", encoding="utf-8")
        if change == "diverged":
            git(output, "commit", "-am", "User commit")
    previous = git(output, "rev-parse", "HEAD")
    result = write_output(settings, task, mirror)
    branch = f"sca/{task.id.hex[:8]}"
    assert result["output_on_side_branch"] is True
    assert result["output_branch"] == branch
    assert git(output, "rev-parse", "HEAD") == previous
    assert git(output, "rev-parse", branch) == result["output_commit"]
    assert git(output, "show", f"{branch}:README.md") == "Agent result"
    assert user_file.read_text(encoding="utf-8") == (
        "Baseline" if change == "branch-switched" else "User work"
    )
    assert write_output(settings, task, mirror) == result


async def test_two_tasks_from_the_same_baseline_cannot_overwrite_each_other(prepared_result, settings):
    output, task, mirror = prepared_result
    other = task.model_copy(update={"id": uuid4()})
    first, second = await asyncio.gather(
        asyncio.to_thread(write_output, settings, task, mirror),
        asyncio.to_thread(write_output, settings, other, mirror),
    )
    applied, fallback = sorted([first, second], key=lambda result: result["output_on_side_branch"])
    assert applied["output_branch"] == "main"
    assert fallback["output_on_side_branch"]
    assert git(output, "rev-parse", "HEAD") == applied["output_commit"] != fallback["output_commit"]
    assert git(output, "rev-parse", fallback["output_branch"]) == fallback["output_commit"]
