import hmac
import json
from datetime import UTC, datetime
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.api import app as api_app
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import Principal, Task, TaskState
from sca.scm.models import PullRequest
from sca.scm.webhook import signed_secret_ref

_GITHUB_REPO = "https://github.com/Acme/App.git"
_GITEA_REPO = "http://forge.test:3000/Acme/App.git"


def issue_payload(repo, *, action="opened", labels=(), comment=None):
    payload = {
        "action": action,
        "repository": {"clone_url": repo, "full_name": "Acme/App", "default_branch": "trunk"},
        "issue": {"number": 23, "title": "Fix calculation", "body": "质检失败",
                  "labels": [{"name": label} for label in labels]},
    }
    if comment is not None:
        payload["comment"] = {"id": 91, "body": comment}
    return payload


def delivery(provider, event, data, secret):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    signature = hmac.digest(secret.encode(), body, "sha256").hex()
    if provider == "github":
        return body, {"X-GitHub-Event": event, "X-Hub-Signature-256": f"sha256={signature}"}
    return body, {"X-Gitea-Event": "issues" if event == "issue_label" else event,
                  "X-Gitea-Event-Type": event, "X-Gitea-Signature": signature}


@pytest.fixture
async def scm_api(tmp_path, monkeypatch):
    secret, other_secret = token_urlsafe(), token_urlsafe()
    settings = Settings(
        _env_file=None, data_dir=tmp_path,
        scm_webhook_secrets={"alpha": {"repo-hook": secret}, "beta": {"elsewhere": other_secret}},
        scm_gitea_hosts={"forge.test:3000": "http://forge.test:3000/api/v1"},
    )
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    integration_id = uuid4()
    defaults = {
        "trigger_labels": ["sca"], "verify_command": "pytest -q", "setup_command": "make setup",
        "image": "example:test", "pull_request": {"draft": True},
    }
    integration = {"id": integration_id, "default_spec": defaults}
    queued = {}

    async def create_task(**values):
        key = values["idempotency_key"]
        if key in queued:
            return queued[key], False
        now = datetime.now(UTC)
        task = Task(
            id=uuid4(), kind=values["kind"], tenant_id=values["tenant_id"], spec=values["spec"],
            result=values["initial_result"], state=TaskState.QUEUED, created_at=now, updated_at=now,
        )
        queued[key] = task
        return task, True

    def row(values):
        now = datetime.now(UTC)
        return dict(values, id=integration_id, tenant_id="alpha", created_at=now, updated_at=now)

    async def replace_integration(identity, values):
        assert identity == integration_id
        return row(values)

    tenant_store = SimpleNamespace(
        find_scm_integration=AsyncMock(return_value=integration), create_task=AsyncMock(side_effect=create_task),
        list_scm_integrations=AsyncMock(return_value=[]), create_scm_integration=AsyncMock(side_effect=row),
        replace_scm_integration=AsyncMock(side_effect=replace_integration), delete_scm_integration=AsyncMock(),
        get_scm_integration=AsyncMock(), get_task=AsyncMock(),
    )
    other_store = SimpleNamespace(
        find_scm_integration=AsyncMock(return_value=None),
        get_task=AsyncMock(side_effect=NotFoundError("Task not found")),
    )
    store = SimpleNamespace(
        for_tenant={"alpha": tenant_store, "beta": other_store}.__getitem__,
        sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(url=make_url(settings.database_url))}),
    )
    users = {
        "admin": Principal(tenant_id="alpha", subject="operator"),
        "member": Principal(tenant_id="alpha", subject="member", role="member"),
        "viewer": Principal(tenant_id="alpha", subject="viewer", role="viewer"),
        "other": Principal(tenant_id="beta", subject="operator"),
        "scoped": Principal(tenant_id="alpha", subject="ci", scopes=["ci:report"]),
    }
    orchestrator = SimpleNamespace(start_task=AsyncMock())
    app = api_app.create_app(
        store=store, orchestrator=orchestrator,
        authenticator=SimpleNamespace(authenticate=AsyncMock(side_effect=users.__getitem__)),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield SimpleNamespace(
            app=app, http=http, store=tenant_store, other_store=other_store, queued=queued,
            secret=secret, other_secret=other_secret, start=orchestrator.start_task,
        )


@pytest.mark.parametrize("provider", ["github", "gitea"])
def test_signature_checks(provider):
    secret = token_urlsafe()
    settings = Settings(_env_file=None, scm_webhook_secrets={"tenant": {"ref": secret}})
    body, headers = delivery(provider, "issues", {"example": "汉字"}, secret)
    signature = headers["X-Hub-Signature-256" if provider == "github" else "X-Gitea-Signature"]
    assert signed_secret_ref(settings, provider, body, signature) == ("tenant", "ref")
    assert signed_secret_ref(settings, provider, body + b" ", signature) is None
    assert signed_secret_ref(settings, provider, body, "sha1=" + signature) is None
    assert signed_secret_ref(settings, provider, body, "é" * 64) is None
    settings.scm_webhook_secrets["other"] = settings.scm_webhook_secrets["tenant"]
    assert signed_secret_ref(settings, provider, body, signature) is None


@pytest.mark.parametrize("provider,repo,event_kind,action", [
    ("github", _GITHUB_REPO, "issues", "labeled"),
    ("gitea", _GITEA_REPO, "issue_label", "label_updated"),
])
async def test_webhook_builds_task_with_defaults_and_deduplicates_label_delivery(
    scm_api, provider, repo, event_kind, action,
):
    path = f"/v1/webhooks/{provider}"
    issue = issue_payload(repo, action=action, labels=["sca"])
    if provider == "github":
        issue["label"] = {"name": "sca"}
    body, headers = delivery(provider, event_kind, issue, scm_api.secret)
    response = await scm_api.http.post(path, content=body, headers=headers)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert len(scm_api.queued) == 1
    task = next(iter(scm_api.queued.values()))
    assert task.tenant_id == "alpha" and task.kind == "coding"
    assert task.spec["repo_url"] == repo and task.spec["base_ref"] == "trunk"
    assert task.spec["push"] and task.spec["pull_request"]["draft"]
    assert task.spec["verify_command"] == "pytest -q" and task.spec["image"] == "example:test"
    assert task.spec["setup_command"] == "make setup"
    assert task.spec["instruction"] == "Fix calculation\n\n质检失败"
    assert task.result["issue"] == {"provider": provider, "number": 23,
                                   "url": repo.removesuffix(".git") + "/issues/23"}
    scm_api.start.assert_awaited_once_with(task_id, "coding")

    task.state = TaskState.SUCCEEDED
    opened, opened_headers = delivery(provider, "issues", issue_payload(repo, labels=["sca"]), scm_api.secret)
    response = await scm_api.http.post(path, content=opened, headers=opened_headers)
    assert response.json()["task_id"] == task_id
    scm_api.start.assert_awaited_once()


@pytest.mark.parametrize("provider,repo", [("github", _GITHUB_REPO), ("gitea", _GITEA_REPO)])
async def test_comment_command_triggers_with_additional_instruction(scm_api, provider, repo):
    issue = issue_payload(repo, action="created", comment="/sca run Add regression tests.\nUse fixtures.")
    body, headers = delivery(provider, "issue_comment", issue, scm_api.secret)
    path = f"/v1/webhooks/{provider}"
    first = await scm_api.http.post(path, content=body, headers=headers)
    second = await scm_api.http.post(path, content=body, headers=headers)
    assert first.json() == second.json()
    assert len(scm_api.queued) == 1
    task = next(iter(scm_api.queued.values()))
    assert task.spec["instruction"].endswith("Add regression tests.\nUse fixtures.")
    # Redelivery while the task is queued can repair a failed workflow start.
    assert scm_api.start.await_count == 2


async def test_webhook_filters_signature_tenant_labels_and_pr_comments(scm_api):
    path = "/v1/webhooks/github"
    opened = issue_payload(_GITHUB_REPO, labels=["sca"])
    body, headers = delivery("github", "issues", opened, scm_api.secret)
    assert (await scm_api.http.post(path, content=body, headers={})).status_code == 401
    assert (await scm_api.http.post(path, content=body + b" ", headers=headers)).status_code == 401
    scm_api.store.find_scm_integration.assert_not_awaited()

    body, headers = delivery("github", "issues", opened, scm_api.other_secret)
    assert (await scm_api.http.post(path, content=body, headers=headers)).json() == {"status": "ignored"}
    scm_api.other_store.find_scm_integration.assert_awaited_once_with(
        "github", "github.com", "acme/app", "elsewhere",
    )
    scm_api.store.find_scm_integration.assert_not_awaited()

    unrelated_label = issue_payload(_GITHUB_REPO, action="labeled", labels=["sca", "docs"])
    unrelated_label["label"] = {"name": "docs"}
    pr_comment = issue_payload(_GITHUB_REPO, action="created", comment="/sca run")
    pr_comment["issue"]["pull_request"] = {"url": "https://github.com/Acme/App/pull/23"}
    for event, data in [
        ("issues", issue_payload(_GITHUB_REPO)), ("issues", unrelated_label),
        ("issue_comment", issue_payload(_GITHUB_REPO, action="created", comment="/sca runner")),
        ("issue_comment", pr_comment),
    ]:
        body, headers = delivery("github", event, data, scm_api.secret)
        assert (await scm_api.http.post(path, content=body, headers=headers)).json() == {"status": "ignored"}
    scm_api.start.assert_not_awaited()
    scm_api.store.create_task.assert_not_awaited()


async def test_integration_crud_requires_admin_and_persists_only_reference(scm_api):
    path = "/v1/integrations/scm"
    body = {"provider": "github", "host": "GitHub.COM", "repo_full_name": "Acme/App",
            "webhook_secret_ref": "repo-hook"}
    for role in ["member", "viewer", "scoped"]:
        headers = {"Authorization": f"Bearer {role}"}
        assert (await scm_api.http.get(path, headers=headers)).status_code == 403
        assert (await scm_api.http.post(path, headers=headers, json=body)).status_code == 403
    admin = {"Authorization": "Bearer admin"}
    response = await scm_api.http.post(path, headers=admin, json=body)
    assert response.status_code == 201
    assert response.json()["host"] == "github.com" and response.json()["repo_full_name"] == "acme/app"
    assert scm_api.secret not in response.text
    assert scm_api.store.create_scm_integration.call_args.args[0]["webhook_secret_ref"] == "repo-hook"
    item = f"{path}/{response.json()['id']}"
    scm_api.store.get_scm_integration.return_value = response.json()
    assert (await scm_api.http.get(item, headers=admin)).status_code == 200
    disabled = await scm_api.http.put(item, headers=admin, json={**body, "enabled": False})
    assert disabled.status_code == 200 and not disabled.json()["enabled"]
    assert (await scm_api.http.delete(item, headers=admin)).status_code == 204
    assert (await scm_api.http.get(path, headers=admin)).json() == []
    assert (await scm_api.http.post(path, headers=admin, json={
        **body, "webhook_secret_ref": "elsewhere",
    })).status_code == 422


async def test_pr_read_uses_authenticated_tenant(scm_api):
    task_id = uuid4()
    url = f"/v1/tasks/{task_id}/pr"
    scm_api.store.get_task.return_value = SimpleNamespace(id=task_id)
    refreshed = PullRequest(provider="github", number=1, url="https://github.com/acme/app/pull/1",
                            state="merged")
    scm_api.app.state.scm_service.refresh_pr = AsyncMock(return_value=refreshed)
    assert (await scm_api.http.get(url, headers={"Authorization": "Bearer other"})).status_code == 404
    scm_api.app.state.scm_service.refresh_pr.assert_not_awaited()
    response = await scm_api.http.get(url, headers={"Authorization": "Bearer viewer"})
    assert response.status_code == 200 and response.json() == refreshed.model_dump()
    scm_api.store.get_task.assert_awaited_once_with(task_id, "alpha")


async def test_scm_integration_applies_dsh_default_to_issue_task(scm_api):
    defaults = {"agent_backend": "dsh", "model": "deepseek-flash"}
    response = await scm_api.http.post("/v1/integrations/scm", headers={"Authorization": "Bearer admin"}, json={
        "provider": "github", "host": "github.com", "repo_full_name": "acme/app",
        "webhook_secret_ref": "repo-hook", "default_spec": defaults,
    })
    assert response.status_code == 201
    scm_api.store.find_scm_integration.return_value = response.json()
    body, headers = delivery("github", "issues", issue_payload(_GITHUB_REPO), scm_api.secret)
    queued = await scm_api.http.post("/v1/webhooks/github", content=body, headers=headers)
    assert queued.status_code == 202
    (task,) = scm_api.queued.values()
    assert {key: task.spec[key] for key in defaults} == defaults
