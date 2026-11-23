from datetime import UTC, datetime
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.billing.metering import task_usage
from sca.config import Settings
from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import Event, Task, TaskState
from sca.core.projects import Project, ProjectInput, ProjectPatch
from sca.db.repo import Store
from sca.db.schema import tasks
from sca.kernel.activities import TurnResult, VerifyResult
from sca.kernel.prompts import render_prompt
from sca.kernel.workflow import ProjectCheckpoints, TaskWorkflow
from sca.kinds.analysis.spec import AnalysisSpec
from sca.kinds.coding.spec import CodingSpec
from sca.projects import prepare_thread_spec, project_usage, thread_group
from sca.storage.local import LocalObjectStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(("state", "metadata", "expected"), [
    ("QUEUED", {}, "working"),
    ("PROVISIONING", {}, "working"),
    ("RUNNING", {}, "working"),
    ("VERIFYING", {}, "working"),
    ("VERIFYING", {"ci_pending": True}, "landing"),
    ("RUNNING", {"result": {"pr": {"state": "open"}}}, "landing"),
    ("AWAITING_REVIEW", {"result": {"pr": {"state": "open"}}}, "ready_for_review"),
    ("AWAITING_REVIEW", {"plan_review": True}, "waiting_on_you"),
    ("AWAITING_REVIEW", {"spec": {"best_of": {"select": "human"}}}, "waiting_on_you"),
    ("AWAITING_REVIEW", {"spec": {"best_of": {"select": "human"}},
                         "result": {"best_of_chosen": 2}}, "ready_for_review"),
    ("PAUSED", {}, "waiting_on_you"),
    ("RUNNING", {"result": {"needs_input": True}}, "waiting_on_you"),
    ("SUCCEEDED", {"result": {"pr": {"state": "open"}}}, "resolved"),
    ("FAILED", {}, "resolved"),
    ("CANCELLED", {}, "resolved"),
    ("IDLE", {}, "idle"),
])
def test_thread_groups(state, metadata, expected):
    assert thread_group({"state": state, "spec": {}, "result": {}, **metadata}) == expected


def test_project_validation_boundaries():
    assert ProjectInput(name="  项目  ", instructions="🙂" * 16000).name == "项目"
    with pytest.raises(ValidationError):
        ProjectInput(name="项目", instructions="🙂" * 16001)
    with pytest.raises(ValidationError):
        ProjectInput(name="   ")
    with pytest.raises(ValidationError):
        ProjectPatch(name=None)
    assert ProjectPatch(default_model=None).model_dump(exclude_unset=True) == {"default_model": None}


def test_migration_sql_adds_rls_and_composite_foreign_keys_before_backfill():
    migration = import_module("sca.db.migrations.versions.0024_projects")
    assert migration.down_revision == "0023"
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output,
    })
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "ALTER TABLE projects ENABLE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))" in sql
    for table in ("tasks", "kb_documents", "memories"):
        assert f"ALTER TABLE {table} ADD COLUMN project_id uuid" in sql
        assert f"CREATE INDEX ix_{table}_project_id ON {table} (project_id)" in sql
    assert sql.count("FOREIGN KEY(tenant_id, project_id) REFERENCES projects (tenant_id, id)") == 3
    assert "WHERE is_default AND deleted_at IS NULL" in sql
    assert "SELECT DISTINCT tenant_id FROM tasks" in sql
    assert "WHERE tasks.tenant_id = projects.tenant_id AND projects.is_default" in sql


def test_admission_and_event_queries_are_correlated_to_task_tenant():
    store = Store(None, tenant_id="example")
    condition = Store._inactive_task_project()
    admission_sql = str(select(tasks.c.id).where(~condition).compile(dialect=postgresql.dialect()))
    assert "FROM projects" in admission_sql
    assert "projects.id = tasks.project_id AND projects.tenant_id = tasks.tenant_id" in admission_sql
    thread_sql = str(store._project_threads(tasks.c.id).compile(dialect=postgresql.dialect()))
    assert thread_sql.count("WHERE events.task_id = tasks.id AND events.tenant_id = tasks.tenant_id") == 2


async def test_store_reuses_the_lazy_default_project_and_blocks_inactive_defaults():
    store = Store(None, tenant_id="team")
    saved = None

    async def execute(statement):
        nonlocal saved
        if statement.is_insert:
            saved = {**statement.compile().params, "status": "active"}
        else:
            sql = str(statement.compile(dialect=postgresql.dialect()))
            assert "projects.is_default" in sql and "FOR UPDATE" in sql
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: saved, one=lambda: saved))

    session = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    first = await store._thread_project(session, "team", None)
    assert first["tenant_id"] == "team" and first["name"] == "默认项目" and first["is_default"]
    assert await store._thread_project(session, "team", None) == first
    assert sum(call.args[0].is_insert for call in session.execute.await_args_list) == 1
    first["status"] = "archived"
    with pytest.raises(ConflictError):
        await store._thread_project(session, "team", None)


def test_prompt_and_repo_defaults_apply_only_to_new_threads():
    project = ProjectInput(
        name="项目", instructions="先运行回归", default_backend="fake", default_model="project-model",
        repos=[{"repo_url": "/first", "base_ref": "develop"}, {"repo_url": "/second", "base_ref": "next"}],
    ).model_dump()
    request = {"instruction": "补测试"}

    def validate(values):
        return CodingSpec.model_validate(values).model_dump()

    first = prepare_thread_spec("coding", request, project, validate)
    assert (first["agent_backend"], first["model"], first["repo_url"], first["base_ref"]) == (
        "fake", "project-model", "/first", "develop",
    )
    assert "## Project instructions\n\n先运行回归\n\n## Thread request\n\n补测试" in render_prompt(first, "initial")
    assert request == {"instruction": "补测试"}
    modified = {**project, "instructions": "更新要求"}
    override = prepare_thread_spec("coding", {
        **request, "repo_url": "/second", "agent_backend": "codex", "model": "custom",
    }, modified, validate)
    assert (override["base_ref"], override["agent_backend"], override["model"]) == ("next", "codex", "custom")
    assert "更新要求" in override["instruction"] and "更新要求" not in first["instruction"]
    with pytest.raises(ValidationError):
        prepare_thread_spec("coding", {"instruction": ""}, project, validate)


def test_project_defaults_apply_to_analysis_without_adding_repo_fields():
    project = ProjectInput(name="数据", instructions="保留统计过程", default_model="model",
                           repos=[{"repo_url": "/repo"}]).model_dump()
    spec = prepare_thread_spec("analysis", {"question": "计算均值", "inputs": [str(uuid4())]}, project,
                               lambda values: AnalysisSpec.model_validate(values).model_dump())
    assert "repo_url" not in spec and spec["model"] == "model"
    assert "## Project instructions\n\n保留统计过程" in render_prompt({**spec, "_kind": "analysis"}, "initial")


def test_project_usage_counts_thread_once_with_subtasks_on_other_models():
    thread_id = uuid4()
    row = {"id": thread_id, "spec": {"model": "main"}, "usage": {"cost_micro_usd": 9400}}
    history = [Event(task_id=thread_id, seq=i, type="agent.usage", created_at=NOW, payload=payload)
               for i, payload in enumerate([
                   {"turn": 1, "model": "main", "input_tokens": 40, "cost_micro_usd": 9000},
                   {"turn": 1, "best_of_candidate": 2, "model": "other", "output_tokens": 23,
                    "cost_micro_usd": 400},
               ], 1)]
    result = project_usage([(row, task_usage(row, history, NOW, "fallback"))], "fallback")
    assert result.totals.tasks == 1 and result.totals.cost_usd == 0.0094
    assert result.by_model["other"].tokens_out == 23
    assert [item.task_id for item in result.by_thread] == [thread_id]
    assert result.by_thread[0].by_model["main"].tokens_in == 40


async def test_running_thread_pauses_after_checkpoint_and_resumes_before_verification(monkeypatch):
    workflow = TaskWorkflow()
    activities = []
    paused = False
    spec = CodingSpec(repo_url="/repo", instruction="需求", auto_finish=True).model_dump()
    spec.update(_project_control=True, hooks={
        "prepare": "prepare", "initial_prompt": "prompt", "checkpoint": "checkpoint",
        "verify": "verify", "publish": "publish",
    })

    async def activity(name, argument, *args, **kwargs):
        nonlocal paused
        activities.append((name, getattr(argument, "state", None)))
        if name == "load_task":
            return spec
        if name == "prompt":
            return "instructions"
        if name == "run_agent_turn":
            paused = True
            return TurnResult(session_id="fixture", final_message="done")
        if name == "project_work_blocked":
            return paused
        if name == "verify":
            return VerifyResult(True, 0, "")

    async def resume_after_wait(*args):
        nonlocal paused
        assert activities[-2:] == [("set_state", "PAUSED"), ("project_work_blocked", None)]
        paused = False

    monkeypatch.setattr("sca.kernel.workflow.workflow.execute_activity", activity)
    monkeypatch.setattr("sca.kernel.workflow.workflow.patched", lambda *_: True)
    monkeypatch.setattr("sca.kernel.workflow.workflow.sleep", resume_after_wait)
    assert await workflow._run("thread-id") == "SUCCEEDED"
    boundary = activities.index(("checkpoint", None))
    assert activities[boundary:boundary + 8] == [
        ("checkpoint", None), ("project_work_blocked", None), ("release_sandbox", None),
        ("set_state", "PAUSED"), ("project_work_blocked", None), ("project_work_blocked", None),
        ("set_state", "RUNNING"), ("restore_sandbox", None),
    ]
    assert activities.index(("verify", None)) > boundary + 7


def test_backfilled_workflow_can_enable_project_checks_after_replay(monkeypatch):
    patched = Mock(side_effect=[False, True])
    monkeypatch.setattr("sca.kernel.workflow.workflow.patched", patched)
    checkpoints = ProjectCheckpoints({"_versioned_prompts": True})
    assert not checkpoints.available()
    assert checkpoints.available()
    assert checkpoints.available()
    assert patched.call_count == 2
    assert patched.call_args_list[0].args != patched.call_args_list[1].args


@pytest.fixture
async def project_api(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    explicit, default = uuid4(), uuid4()
    project = Project(
        id=explicit, tenant_id="team", name="线程项目", instructions="检查现有行为",
        status="active", is_default=False, created_by="member", created_at=NOW, updated_at=NOW,
        repos=[{"repo_url": "/repo", "base_ref": "develop"}], default_backend="fake",
    )
    scoped = SimpleNamespace(
        list_projects=AsyncMock(return_value=[]), create_project=AsyncMock(return_value=project),
        get_project=AsyncMock(return_value=project), update_project=AsyncMock(return_value=project),
        delete_project=AsyncMock(return_value=[]), change_project_status=AsyncMock(),
    )

    async def change_status(project_id, action):
        assert project_id == explicit
        project.status = {"pause": "paused", "archive": "archived"}.get(action, "active")
        return project

    async def create_task(*, tenant_id, kind, spec, project_id, validate_spec, **kwargs):
        if project_id is not None and project_id != explicit:
            raise NotFoundError("项目不存在")
        if project_id is not None and project.status != "active":
            raise ConflictError("项目已暂停或归档，无法创建线程")
        effective = prepare_thread_spec(kind, spec, project.model_dump(), validate_spec)
        return Task(id=uuid4(), project_id=project_id or default, tenant_id=tenant_id,
                    kind=kind, spec=effective, state=TaskState.QUEUED,
                    created_at=NOW, updated_at=NOW), True

    scoped.change_project_status.side_effect = change_status
    scoped.create_task = AsyncMock(side_effect=create_task)
    store = SimpleNamespace(
        for_tenant=Mock(return_value=scoped),
        sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(url=make_url("postgresql://local/db"))}),
    )
    orchestrator = SimpleNamespace(start_task=AsyncMock())
    app = create_app(
        store=store, orchestrator=orchestrator, object_store=LocalObjectStore(tmp_path),
        authenticator=StaticTokenAuthenticator({"member": "team:member", "viewer": "team:viewer", "admin": "team"}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, scoped, orchestrator, explicit, default


async def test_project_http_routes_delegate_with_roles_and_prepare_task(project_api):
    client, store, orchestrator, explicit, default = project_api
    member = {"Authorization": "Bearer member"}
    viewer = {"Authorization": "Bearer viewer"}
    admin = {"Authorization": "Bearer admin"}
    url = f"/v1/projects/{explicit}"
    assert (await client.get("/v1/projects", headers=viewer)).status_code == 200
    assert (await client.post("/v1/projects", headers=viewer, json={"name": "项目"})).status_code == 403
    assert (await client.post("/v1/projects", headers=member, json={"name": "项目"})).status_code == 201
    assert (await client.get(url, headers=viewer)).json()["name"] == "线程项目"
    assert (await client.patch(url, headers=member, json={"instructions": "新"})).status_code == 200
    store.update_project.assert_awaited_once_with(explicit, {"instructions": "新"})
    body = {"kind": "coding", "spec": {"instruction": "补测试"}, "project_id": str(explicit)}
    assert (await client.post(url + "/pause", headers=viewer)).status_code == 403
    for action, status in (("pause", "paused"), ("archive", "archived"),
                           ("unarchive", "active"), ("resume", "active")):
        result = await client.post(url + "/" + action, headers=member)
        assert result.status_code == 200 and result.json()["status"] == status
        if status != "active":
            assert (await client.post("/v1/tasks", headers=member, json=body)).status_code == 409
    posted = await client.post("/v1/tasks", headers=member, json=body)
    assert posted.status_code == 201 and posted.json()["project_id"] == str(explicit)
    assert posted.json()["spec"]["base_ref"] == "develop"
    assert "检查现有行为" in posted.json()["spec"]["instruction"]
    body.pop("project_id")
    assert (await client.post("/v1/tasks", headers=member, json=body)).json()["project_id"] == str(default)
    assert orchestrator.start_task.await_count == 2
    assert (await client.delete(url, headers=member)).status_code == 403
    assert (await client.delete(url, headers=admin)).status_code == 204


def test_project_asset_uses_role_checks_and_existing_task_routes():
    from pathlib import Path

    static = Path("src/sca/api/static")
    page = (static / "portal.html").read_text()
    navigation = (static / "portal-nav.js").read_text()
    source = (static / "portal-projects.js").read_text()
    composer = (static / "portal-tasks.js").read_text()
    assert "Object.hasOwn(pages, requested) ? requested : 'home'" in navigation
    assert 'id="project-create" class="primary" type="button" data-access="member" hidden' in page
    assert 'aria-label="线程面包屑"' in page and "portalProjects.breadcrumb(t)" in page
    assert 'href="#/projects" data-nav="projects" data-access="tenant" hidden' in page
    assert 'href="#/tasks" data-nav="tasks" data-access="history" hidden' in page
    for value in ('待审核', '等你处理', '进行中', '合入中', '空闲', '已完成', '文件库', '用量', '设置', '16,000'):
        assert value in source
    assert "$('project-new-task').hidden = project.status !== 'active' || !ui.can('member')" in source
    assert "if (project.status !== 'active' || !ui.can('member')) return;" in source
    assert "if (!ui.can('admin')) return;" in source
    assert "project_id: project.id" in composer
    assert all(api not in source for api in ("innerHTML", "outerHTML", "insertAdjacentHTML"))
