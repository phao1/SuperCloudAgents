import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from sca.config import Settings
from sca.core.models import ExecExit, RepoCredential, Task, TaskState
from sca.intents.base import IntentSource
from sca.intents.compiler import compile_sources
from sca.intents.remote import RemoteClient, RemoteError
from sca.intents.tickets import Outcome, ticket_client
from sca.intents.tokens import resolve_tokens
from sca.kernel.activities import KernelActivities, StepInput
from sca.kernel.workflow import TaskWorkflow
from sca.kinds.coding.activities import CodingActivities
from sca.kinds.coding.workflow import CodingTaskWorkflow, workflow

PNG = b"\x89PNG\r\n\x1a\n"
PR = "https://github.com/acme/website/pull/9"
JIRA = {
    "type": "jira",
    "name": "checkout",
    "params": {
        "site": "https://acme.atlassian.net",
        "issue_key": "WEB-1",
        "token_secret": "bot",
        "acceptance_field": "customfield_10037",
        "transition_id": "31",
    },
}


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        figma_api_url="https://figma.test",
        intent_credentials={
            "acme": {
                "figma/designs": {"token": "figma-fixture"},
                "jira/bot": {
                    "token": "jira-fixture",
                    "site": "https://acme.atlassian.net",
                    "email": "bot@example.invalid",
                },
                "linear/bot": {"token": "linear-fixture"},
            }
        },
    )


def test_remote_reference_uses_params_only():
    assert IntentSource.model_validate(JIRA).path is None
    with pytest.raises(ValueError, match="remote intents"):
        IntentSource.model_validate({**JIRA, "file_id": "abc"})
    with pytest.raises(ValueError, match="exactly one"):
        IntentSource(type="design_tokens", name="tokens")


def test_outcome_includes_pr_link_for_internal_git_host():
    link = "http://git.example.invalid:3000/acme/site/pulls/1"
    task = SimpleNamespace(
        id=uuid4(), state=TaskState.SUCCEEDED, result={"pull_request": {"html_url": link}}
    )
    assert Outcome.from_task(task).pr_url == link


async def test_figma_compile_through_kernel(tmp_path, monkeypatch, settings, caplog):
    figma = {
        "type": "figma",
        "name": "frame",
        "params": {
            "file_key": "file123",
            "node_ids": ["1-2"],
            "token_secret": "designs",
            "visual": {
                "1:2": {
                    "start_command": "npm run dev",
                    "base_url": "http://localhost:3000",
                    "page_url": "http://localhost:3000/checkout",
                }
            },
        },
    }
    colors = {"r": 1, "g": 0, "b": 0, "a": 1}
    frame = {
        "name": "Checkout",
        "type": "FRAME",
        "absoluteBoundingBox": {"width": 800, "height": 600},
        "layoutMode": "VERTICAL",
        "itemSpacing": 16,
        "children": [
            {
                "name": "Buy",
                "type": "TEXT",
                "characters": "Pay now",
                "fills": [{"type": "SOLID", "color": colors}],
                "style": {"fontFamily": "Inter", "fontSize": 16, "fontWeight": 600},
            },
            {"name": "Submit", "type": "INSTANCE", "componentId": "1:50"},
        ],
    }
    visits = []

    def handler(req):
        visits.append(req.url.path)
        if req.url.host.endswith("amazonaws.com"):
            assert "x-figma-token" not in req.headers
            return httpx.Response(200, content=PNG)
        assert req.url.host == "figma.test"
        assert req.headers["x-figma-token"] == "figma-fixture"
        if req.url.path.endswith("/nodes"):
            assert req.url.params["ids"] == "1:2"
            return httpx.Response(
                200,
                json={
                    "version": "42",
                    "nodes": {
                        "1:2": {
                            "document": frame,
                            "components": {"1:50": {"name": "Button", "key": "c"}},
                        }
                    },
                },
            )
        if req.url.path == "/v1/images/file123":
            assert dict(req.url.params) == {
                "ids": "1:2",
                "version": "42",
                "format": "png",
                "scale": "1",
            }
            return httpx.Response(
                200,
                json={
                    "images": {
                        "1:2": "https://figma-alpha-api.s3.us-west-2.amazonaws.com/render.png?signature=fixture"
                    }
                },
            )
        assert req.url.path.endswith("/variables/local")
        return httpx.Response(
            200,
            json={
                "meta": {
                    "variableCollections": {"c": {"name": "Global", "defaultModeId": "light"}},
                    "variables": {
                        "a": {
                            "name": "color",
                            "variableCollectionId": "c",
                            "resolvedType": "COLOR",
                            "valuesByMode": {"light": colors},
                        },
                        "b": {
                            "name": "color/brand",
                            "variableCollectionId": "c",
                            "resolvedType": "COLOR",
                            "valuesByMode": {"light": {"type": "VARIABLE_ALIAS", "id": "a"}},
                        },
                        "c": {
                            "name": "spacing",
                            "variableCollectionId": "c",
                            "resolvedType": "FLOAT",
                            "scopes": ["GAP"],
                            "valuesByMode": {"light": 16},
                        },
                    },
                }
            },
        )

    @asynccontextmanager
    async def connection(conf, tenant_id, secrets):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield RemoteClient(client, conf, tenant_id, secrets)

    monkeypatch.setattr("sca.intents.remote.remote_connection", connection)
    caplog.set_level("INFO", logger="httpx")
    kernel = object.__new__(KernelActivities)
    kernel.settings = settings
    kernel.secrets = AsyncMock()
    kernel.store = AsyncMock()
    kernel.store.get_task.return_value = SimpleNamespace(
        spec={"intents": [figma]}, result={}, tenant_id="acme"
    )
    kernel._workspace = Mock(return_value=SimpleNamespace(workspace=tmp_path))
    kernel.objects = AsyncMock()
    result = await kernel.compile_intents("task")
    assert result["checks"][0]["name"] == "frame.1"
    context = tmp_path / ".sca/context"
    assert (context / "frame.1-baseline.png").read_bytes() == PNG
    assert "viewport" in (context / "frame.1.spec.cjs").read_text()
    assert '"width": 800, "height": 600' in (context / "frame.1.spec.cjs").read_text()
    notes = (context / "frame-design.md").read_text()
    assert all(
        text in notes
        for text in [
            "Pay now",
            "800 × 600",
            "fontFamily=Inter",
            "#ff0000",
            "layoutMode=VERTICAL",
            "component='Button'",
        ]
    )
    variables = json.loads((context / "frame-tokens.json").read_text())
    tokens = resolve_tokens(variables)
    assert tokens["Global.color.brand.$root"].value == tokens["Global.color.$root"].value
    assert tokens["Global.spacing.$root"].value == {"value": 16, "unit": "px"}
    assert kernel.store.append_event.call_args.args[2]["sources"][0]["checks"] == ["frame.1"]
    assert "signature=fixture" not in caplog.text
    assert visits == [
        "/v1/files/file123/nodes",
        "/v1/images/file123",
        "/render.png",
        "/v1/files/file123/variables/local",
    ]


async def test_figma_discovery_with_variables_denied(tmp_path, settings):
    def handler(req):
        if req.url.path == "/v1/files/key":
            assert req.url.params["depth"] == "3"
            return httpx.Response(
                200,
                json={
                    "document": {
                        "type": "DOCUMENT",
                        "children": [
                            {
                                "type": "CANVAS",
                                "children": [
                                    {"type": "FRAME", "id": "1:2"},
                                    {"type": "FRAME", "id": "1:3", "visible": False},
                                ],
                            }
                        ],
                    }
                },
            )
        if req.url.path.endswith("/nodes"):
            assert req.url.params["ids"] == "1:2"
            return httpx.Response(200, json={"nodes": {"1:2": {"document": {"type": "FRAME"}}}})
        if req.url.path.startswith("/v1/images"):
            return httpx.Response(
                200, json={"images": {"1:2": "https://s3-alpha-sig.figma.com/img"}}
            )
        if req.url.path == "/img":
            return httpx.Response(200, content=PNG)
        return httpx.Response(403, json={"error": "file_variables:read"})

    source = IntentSource(
        type="figma", name="frame", params={"file_key": "key", "token_secret": "designs"}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteClient(client, settings, "acme")
        bundle = await compile_sources([source], tmp_path, AsyncMock(), "acme", remote=remote)
    assert not bundle.checks
    assert set(bundle.context_files) == {"frame.1-baseline.png", "frame-design.md"}
    assert "variables are unavailable" in bundle.context_files["frame-design.md"].decode()


async def test_jira_read_and_write_outcome(tmp_path, settings):
    def adf(text):
        return {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }

    written = []

    def handler(req):
        expected = base64.b64encode(b"bot@example.invalid:jira-fixture").decode()
        assert req.headers["authorization"] == "Basic " + expected
        if req.method == "POST":
            written.append((req.url.path, json.loads(req.content)))
            return (
                httpx.Response(204)
                if req.url.path.endswith("transitions")
                else httpx.Response(201, json={"id": "13"})
            )
        if req.url.path.endswith("/issue/WEB-1"):
            assert "customfield_10037" in req.url.params["fields"]
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "summary": "Checkout",
                        "description": adf("Show the total"),
                        "customfield_10037": adf("Round tax correctly"),
                        "attachment": [{"id": "23", "filename": "target.png"}],
                    }
                },
            )
        if req.url.path.endswith("/comment"):
            return httpx.Response(
                200,
                json={
                    "comments": [{"body": adf("Check delivery charges")}],
                    "total": 1,
                },
            )
        assert req.url.path.endswith("/attachment/content/23")
        assert req.url.params["redirect"] == "false"
        return httpx.Response(200, content=PNG)

    source = IntentSource.model_validate(JIRA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteClient(client, settings, "acme")
        bundle = await compile_sources([source], tmp_path, AsyncMock(), "acme", remote=remote)
        jira = ticket_client(source, remote)
        await jira.comment(Outcome("Task succeeded", PR))
        assert await jira.transition()
    note = bundle.context_files["checkout-ticket.md"].decode()
    assert all(
        text in note
        for text in [
            "Checkout",
            "Show the total",
            "Round tax correctly",
            "Check delivery charges",
            "checkout-attachment-1.png",
        ]
    )
    assert bundle.context_files["checkout-attachment-1.png"] == PNG
    assert written[0][0].endswith("/comment")
    assert written[0][1]["body"]["content"][0]["content"][2]["marks"][0]["attrs"]["href"] == PR
    assert written[1][1] == {"transition": {"id": "31"}}


@pytest.mark.parametrize(
    ("tenant", "site"), [("other", JIRA["params"]["site"]), ("acme", "https://other.atlassian.net")]
)
async def test_jira_credential_cannot_cross_tenant_or_site(tmp_path, settings, tenant, site):
    handler = Mock(side_effect=AssertionError("no request should be made"))
    source = IntentSource.model_validate({**JIRA, "params": {**JIRA["params"], "site": site}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteClient(client, settings, tenant)
        with pytest.raises(RemoteError, match="configured"):
            await compile_sources([source], tmp_path, AsyncMock(), tenant, remote=remote)
    handler.assert_not_called()


async def test_linear_paginated_ticket_attachments_and_graphql_writeback(tmp_path, settings):
    asset = "https://uploads.linear.app/workspace/image.png"
    posted = []

    def connection(nodes, cursor=None):
        return {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        }

    def handler(req):
        assert req.headers["authorization"] == "linear-fixture"
        if req.url.host == "uploads.linear.app":
            return httpx.Response(200, content=PNG)
        assert req.url == httpx.URL("https://api.linear.app/graphql")
        data = json.loads(req.content)
        query = data["query"]
        variables = data["variables"]
        if "query IntentIssue(" in query:
            assert variables == {"id": "WEB-1"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issue": {
                            "id": "canonical",
                            "title": "Checkout",
                            "description": f"## Acceptance criteria\n- Show the total\n![design]({asset})",
                            "comments": connection([{"body": "First"}], "next"),
                            "attachments": connection(
                                [{"title": "Reference", "url": "https://other.test/ref"}]
                            ),
                        }
                    }
                },
            )
        if "IntentPage" in query:
            assert variables == {"id": "canonical", "after": "next"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issue": {
                            "comments": connection([{"body": "Second"}]),
                        }
                    }
                },
            )
        if "IntentIssueId" in query:
            return httpx.Response(200, json={"data": {"issue": {"id": "canonical"}}})
        posted.append(variables)
        field = "commentCreate" if "IntentComment" in query else "issueUpdate"
        return httpx.Response(200, json={"data": {field: {"success": True}}})

    source = IntentSource(
        type="linear",
        name="linear",
        params={
            "issue_id": "WEB-1",
            "token_secret": "bot",
            "state_id": "done",
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteClient(client, settings, "acme")
        bundle = await compile_sources([source], tmp_path, AsyncMock(), "acme", remote=remote)
        linear = ticket_client(source, remote)
        await linear.comment(Outcome("Task succeeded", PR))
        assert await linear.transition()
    note = bundle.context_files["linear-ticket.md"].decode()
    assert all(
        text in note
        for text in [
            "Checkout",
            "Acceptance criteria",
            "Show the total",
            "First",
            "Second",
            "https://other.test/ref",
        ]
    )
    assert bundle.context_files["linear-attachment-2.png"] == PNG
    assert posted[0]["input"]["issueId"] == "canonical"
    assert PR in posted[0]["input"]["body"]
    assert posted[1] == {"id": "canonical", "input": {"stateId": "done"}}


async def test_github_ticket_reads_and_posts_with_tenant_repo_credential(tmp_path, settings):
    signed_url = "https://private-user-images.githubusercontent.com/file.png?jwt=fixture"
    posted = []

    def handler(req):
        if req.url.host == "private-user-images.githubusercontent.com":
            assert str(req.url) == signed_url
            assert "authorization" not in req.headers
            return httpx.Response(200, content=PNG)
        assert req.url.host == "api.github.com"
        assert req.headers["authorization"] == "Bearer github-fixture"
        if req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 15})
        if req.url.path.endswith("/comments"):
            assert req.url.params["page"] == "1"
            return httpx.Response(200, json=[{"body": "Ticket comment"}])
        assert req.url.path == "/repos/acme/website/issues/5"
        return httpx.Response(
            200,
            json={
                "title": "Checkout",
                "body": f"## Acceptance criteria\n- Show the total\n![attachment]({signed_url})",
            },
        )

    source = IntentSource(
        type="github_issue", name="gh", params={"repo": "ACME/website", "number": 5}
    )
    secrets = AsyncMock()
    secrets.repo_credential.return_value = RepoCredential(
        username="ignored", password="github-fixture"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteClient(client, settings, "acme", secrets)
        bundle = await compile_sources([source], tmp_path, AsyncMock(), "acme", remote=remote)
        await ticket_client(source, remote).comment(Outcome("Task succeeded", PR))
    assert bundle.context_files["gh-attachment-1.png"] == PNG
    notes = bundle.context_files["gh-ticket.md"].decode()
    assert "Ticket comment" in notes
    assert "jwt=fixture" not in notes
    assert PR in posted[0]["body"]
    secrets.repo_credential.assert_awaited_with("acme", "https://github.com/acme/website")


async def test_ticket_writeback_records_partial_failure_and_resumes(monkeypatch, settings):
    actions = []

    def handler(req):
        assert req.method == "POST"
        if req.url.path.endswith("/comment"):
            actions.append("comment")
            assert PR in req.content.decode()
            return httpx.Response(201, json={"id": "13"})
        actions.append("transition")
        return httpx.Response(503 if actions.count("transition") == 1 else 204)

    @asynccontextmanager
    async def connection(conf, tenant_id, secrets):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield RemoteClient(client, conf, tenant_id, secrets)

    monkeypatch.setattr("sca.intents.remote.remote_connection", connection)
    task = Task(
        id=uuid4(),
        tenant_id="acme",
        kind="coding",
        spec={"intents": [JIRA]},
        state=TaskState.SUCCEEDED,
        result={"pr_url": PR},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def update(_task_id, change):
        task.result.update(change)

    activities = object.__new__(CodingActivities)
    activities.settings = settings
    activities.secrets = Mock()
    activities.store = AsyncMock()
    activities.store.get_task.return_value = task
    activities.store.update_result.side_effect = update
    await activities.write_ticket_outcome(str(task.id))
    assert task.result["ticket_writeback"]["checkout"] == {
        "comment_posted": True,
        "error": "jira: HTTP 503",
    }
    await activities.write_ticket_outcome(str(task.id))
    assert task.result["ticket_writeback"]["checkout"] == {
        "completed": True,
        "comment_posted": True,
        "transitioned": True,
    }
    await activities.write_ticket_outcome(str(task.id))
    assert actions == ["comment", "transition", "transition"]
    assert activities.store.append_event.await_count == 2


@pytest.mark.parametrize("state", [TaskState.FAILED, TaskState.CANCELLED])
async def test_unsuccessful_outcomes_post_without_transition(monkeypatch, settings, state):
    def handler(req):
        assert req.url.path.endswith("/comment")
        assert state.value in req.content.decode()
        assert "Pull request" not in req.content.decode()
        return httpx.Response(201, json={"id": "13"})

    @asynccontextmanager
    async def connection(conf, tenant_id, secrets):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield RemoteClient(client, conf, tenant_id, secrets)

    monkeypatch.setattr("sca.intents.remote.remote_connection", connection)
    task = Task(
        id=uuid4(),
        tenant_id="acme",
        kind="coding",
        spec={"intents": [JIRA]},
        state=state,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    activities = object.__new__(CodingActivities)
    activities.settings = settings
    activities.secrets = Mock()
    activities.store = AsyncMock()
    activities.store.get_task.return_value = task
    await activities.write_ticket_outcome(str(task.id))
    status = activities.store.append_event.await_args.args[2]
    assert status["comment_posted"] and status["completed"]


@pytest.mark.parametrize("error", [None, ApplicationError("failed"), asyncio.CancelledError()])
async def test_coding_workflow_runs_writeback_on_terminal_paths(monkeypatch, error):
    async def run(self, task_id):
        self._task_spec = {"intents": [JIRA]}
        if error:
            raise error
        return "SUCCEEDED"

    execute = AsyncMock()
    monkeypatch.setattr(TaskWorkflow, "run", run)
    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", lambda patch: patch == "coding-ticket-writeback")
    if error:
        with pytest.raises(type(error)):
            await CodingTaskWorkflow().run("task")
    else:
        assert await CodingTaskWorkflow().run("task") == "SUCCEEDED"
    assert execute.await_args.args == ("coding.write_ticket_outcome", "task")


async def test_coding_verification_passes_git_base_as_an_argument():
    base = "a" * 40
    kernel = object.__new__(KernelActivities)
    kernel.store = AsyncMock()
    kernel.store.get_task.return_value = SimpleNamespace(
        kind="coding",
        result={
            "base_sha": base,
            "intents": {
                "checks": [
                    {
                        "name": "tokens",
                        "command": 'python token_check.py cfg "$1"',
                        "needs_base_sha": True,
                    }
                ]
            },
        },
    )
    kernel._sandbox_id = AsyncMock(return_value="sandbox")
    kernel._run = AsyncMock(return_value=(ExecExit(exit_code=0), ""))
    assert (await kernel.verify_intents(StepInput("task", 1))).passed
    kernel._run.assert_awaited_once_with(
        "sandbox",
        ["bash", "-lc", 'python token_check.py cfg "$1"', "sca-intent", base],
        timeout_s=25 * 60,
    )
