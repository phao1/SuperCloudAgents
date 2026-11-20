import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from sca.agents.opencode import OpenCodeBackend
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import ExecExit, ExecOutput, SandboxSpec, Task
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kinds import builtin
from sca.kinds.coding.spec import CodingSpec
from sca.sandbox.docker import DockerSandboxProvider
from sca.sandbox.egress import render_squid_conf
from sca.secrets import OpenBaoSecretProvider
from sca.storage.local import LocalObjectStore
from sca.tools.models import ToolDefinition
from sca.tools.sandbox import McpSandboxSpec, ToolSandboxProvider

builtin.load()


@pytest.mark.parametrize("changes", [
    {"url": "https://127.0.0.1/mcp"},
    {"url": "https://user:password@tickets.example.org/mcp"},
    {"url": "https://tickets.example.org:9000/mcp"},
    {"url": "http://tickets.example.org/mcp", "headers": {"X-Key": "secret-ref"}},
    {"headers": {"Host": "secret-ref"}},
    {"headers": {"X-Key": "secret-ref", "x-key": "duplicate"}},
    {"headers": {"X-Key": "literal secret value"}},
    {"name": "knowledge"},
    {"name": "available"},
])
def test_invalid_remote_registration(changes):
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate({
            "name": "tickets", "url": "https://tickets.example.org/mcp", **changes,
        })


class TaskStore:
    tenant_id = "tenant-a"

    def __init__(self, backend: str = "opencode") -> None:
        now = datetime.now(UTC)
        self.task = Task(id=uuid4(), tenant_id=self.tenant_id, kind="coding", state="RUNNING",
                         spec={"repo_url": "/repo", "instruction": "work", "tools": ["tickets"],
                               "agent_backend": backend}, created_at=now, updated_at=now)
        self.events = {}
        self.allowed = True
        self.tool = {"name": "tickets", "tenant_id": self.tenant_id, "kind": "remote",
                     "url": "https://tickets.example.org/mcp", "description": "Tickets",
                     "headers": {"Authorization": "auth-header"}, "enabled": True,
                     "version": 4, "created_by": "admin", "created_at": now}

    async def get_task(self, task_id):
        assert str(task_id) == str(self.task.id)
        return self.task

    def for_tenant(self, tenant_id):
        # "eval" holds platform champions; this fake has none.
        assert tenant_id in (self.tenant_id, "eval")
        return self

    async def get_champion_rollout(self, agent_version, model):
        return None

    async def task_submitter(self, task_id):
        assert task_id == self.task.id
        return "alice"

    async def available_tools(self, actor, teams, *, names):
        assert (actor, teams, names) == ("alice", ["ops"], ["tickets"])
        return [self.tool] if self.allowed else []

    async def update_result(self, task_id, values):
        (await self.get_task(task_id)).result.update(values)

    async def append_event(self, task_id, type_, payload, *, dedup_key):
        await self.get_task(task_id)
        self.events.setdefault(dedup_key, (type_, payload))

    async def get_task_champion(self, task_id):
        return None

    async def pin_task_champion(self, task_id, selection):
        return None

    async def resolve_task_opencode_version(self, task_id, default):
        return default

    async def get_session_id(self, task_id):
        return None

    async def set_session(self, task_id, backend, session_id):
        assert backend == "opencode" and session_id == "session"

    async def add_usage(self, task_id, usage):
        assert usage["input_tokens"] == 2


async def test_tool_registry_authorization_for_tenant_and_platform_tools(tmp_path):
    db = TaskStore()
    scoped = SimpleNamespace(tenant_id=db.tenant_id,
                             create_tool=AsyncMock(return_value=db.tool),
                             get_tool=AsyncMock(return_value={"tenant_id": "platform"}),
                             add_tool_grant=AsyncMock(return_value={
                                 "tenant_id": db.tenant_id, "tool_tenant_id": "platform",
                                 "tool_name": "knowledge", "subject_type": "tenant",
                                 "subject": db.tenant_id, "granted_by": "static-token",
                             }))
    fake_store = SimpleNamespace(for_tenant=Mock(return_value=scoped), sessionmaker=SimpleNamespace(
        kw={"bind": SimpleNamespace(url=make_url("postgresql://local/db"))},
    ))
    app = create_app(store=fake_store, object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator(
                         {"admin": db.tenant_id, "member": f"{db.tenant_id}:member"},
                     ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tool = {"name": "tickets", "url": db.tool["url"], "headers": {"Authorization": "MCP_TOKEN"}}
        assert (await client.post("/v1/tools", json=tool,
                                  headers={"Authorization": "Bearer member"})).status_code == 403
        admin = {"Authorization": "Bearer admin"}
        result = await client.post("/v1/tools", json=tool, headers=admin)
        assert result.status_code == 201 and result.json()["version"] == 4
        assert scoped.create_tool.call_args.args[0]["headers"] == {"Authorization": "MCP_TOKEN"}
        grant = {"subject_type": "tenant", "subject": db.tenant_id}
        assert (await client.post("/v1/tools/shared/grants", json=grant, headers=admin)).status_code == 403
        assert (await client.post("/v1/tools/knowledge/grants", json=grant, headers=admin)).status_code == 201
        assert scoped.add_tool_grant.await_count == 1
        assert scoped.add_tool_grant.call_args.args[0]["tenant_id"] == db.tenant_id


async def test_task_attaches_mcp_config_allowlist_and_version_without_recording_credentials(
    tmp_path, monkeypatch,
):
    from sca.tools.settings import ToolSettings

    monkeypatch.setattr("sca.tools.settings.get_tool_settings", lambda: ToolSettings(
        tool_teams={"tenant-a": {"alice": ["ops"]}},
    ))
    settings = Settings(_env_file=None, data_dir=tmp_path, pip_cache=False)
    version = tmp_path / "2.3.0-sca.1"
    version.mkdir()
    (version / "opencode").touch()
    credential = "Bearer simulated-ticket-credential"
    read = Mock(return_value={"data": {"data": {"value": credential}}})
    client = SimpleNamespace(secrets=SimpleNamespace(
        kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=read)),
    ))
    store = TaskStore()

    class Sandbox:
        async def create(self, name, spec):
            assert isinstance(spec, McpSandboxSpec)
            assert spec.network == "restricted" and spec.mcp_hosts == ["tickets.example.org"]
            assert credential not in repr(spec)
            return name

        async def mcp_model_url(self, sandbox_id, url, peer):
            assert url == "http://gateway/v1"
            return "http://isolated-gateway/v1"

        async def exec(self, sandbox_id, argv, *, stdin, env, **kwargs):
            assert credential not in " ".join(argv) and credential not in repr(env)
            config = json.loads(stdin)
            assert config["permission"] == {"websearch": "deny", "webfetch": "deny"}
            assert config["providers"]["sca"]["settings"]["baseURL"] == "http://isolated-gateway/v1"
            assert config["mcp"] == {"servers": {"tickets": {
                "type": "remote", "url": "https://tickets.example.org/mcp", "oauth": False,
                "headers": {"Authorization": credential},
            }}}
            for line in (
                {"type": "text", "sessionID": "session", "part": {"text": credential}},
                {"type": "tool_use", "part": {"tool": "tickets_lookup", "state": {
                    "output": credential.removeprefix("Bearer "),
                }}},
                {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 2}}},
            ):
                yield ExecOutput(stream="stdout", text=json.dumps(line))
            yield ExecExit(exit_code=0)

    class Gateway:
        sandbox_url = "http://gateway/v1"

        async def issue_key(self, *args, **kwargs):
            return "simulated-model-key"

        async def revoke_key(self, alias):
            return None

    kernel = KernelActivities(
        settings=settings, store=store, sandbox=Sandbox(),
        agent=OpenCodeBackend(tmp_path, version.name, "model"),
        objects=LocalObjectStore(tmp_path / "objects"), gateway=Gateway(),
        secrets=OpenBaoSecretProvider(settings, client=client),
    )
    env = ActivityEnvironment()
    task_id = str(store.task.id)
    assert (await env.run(kernel.load_task, task_id))["tools"] == ["tickets"]
    await env.run(kernel.create_sandbox, task_id)
    result = await env.run(kernel.run_agent_turn, TurnInput(task_id, 1, "hello"))
    assert result.final_message == "<redacted>"
    assert store.events["turn1.attempt1.tools_attached"] == (
        "tools.attached", {"tools": [{"name": "tickets", "version": 4}]},
    )
    assert credential.removeprefix("Bearer ") not in repr(store.events)
    assert read.call_args.kwargs["path"] == "sca/tenants/tenant-a/tools/tickets/auth-header"
    store.tool["url"] = "https://changed.example.org/mcp"
    with pytest.raises(ApplicationError, match="MCP 主机配置已更新"):
        await env.run(kernel.run_agent_turn, TurnInput(task_id, 2, "again"))
    store.allowed = False
    with pytest.raises(ApplicationError, match="工具不可用或未授权"):
        await env.run(kernel.run_agent_turn, TurnInput(task_id, 2, "again"))
    assert read.call_count == 1


@pytest.mark.parametrize("backend", ["codex", "pi", "opencode-brain"])
async def test_unsupported_backends_fail_at_task_start(tmp_path, backend):
    store = TaskStore(backend)
    kernel = KernelActivities(
        settings=Settings(_env_file=None, data_dir=tmp_path), store=store, sandbox=object(),
        agent=SimpleNamespace(name=backend), objects=LocalObjectStore(tmp_path),
    )
    with pytest.raises(ApplicationError, match=f"{backend} 不支持 tools") as error:
        await kernel.load_task(str(store.task.id))
    assert error.value.non_retryable
    assert CodingSpec(repo_url="/repo", instruction="work").tools == []


async def test_docker_tool_network_is_isolated_and_cleanup_preserves_shared_proxy(tmp_path, monkeypatch):
    calls = []

    async def cli(self, *args, check=True):
        calls.append(args)
        return (1, "", "not found") if args[0] == "inspect" else (0, "", "")

    async def create(self, name, spec):
        calls.append(("create", name, self.network_name, list(self.egress_allowlist), spec.labels))
        return f"sca-{name}"

    async def address(self, peer):
        assert self.network_name != "sca-sandbox-net" and peer == "gateway"
        return "172.28.0.5"

    monkeypatch.setattr(DockerSandboxProvider, "_cli", cli)
    monkeypatch.setattr(DockerSandboxProvider, "create", create)
    monkeypatch.setattr(DockerSandboxProvider, "address_of", address)
    provider = DockerSandboxProvider(egress_allowlist=["pypi.org"], peers=["gateway"],
                                     egress_ca_dir=tmp_path)
    sandbox = ToolSandboxProvider(provider)
    plain = await sandbox.create("task-plain", SandboxSpec(image="image"))
    added = await sandbox.create("task-ticket", McpSandboxSpec(
        image="image", mcp_hosts=["tickets.example.org"],
    ))
    created = [call for call in calls if call[0] == "create"]
    assert created[0][2:4] == ("sca-sandbox-net", ["pypi.org"])
    assert created[1][2:4] == (f"{added}-sandbox-net", ["pypi.org", "tickets.example.org"])
    assert "http_access deny private" in render_squid_conf(created[1][3])
    assert await sandbox.mcp_model_url(added, "http://172.18.0.4:4000/v1", "gateway") == (
        "http://172.28.0.5:4000/v1"
    )
    await sandbox.destroy(plain)
    await sandbox.destroy(added)
    assert ("network", "rm", f"{added}-sandbox-net") in calls
    assert ("network", "disconnect", "-f", f"{added}-sandbox-net", "gateway") in calls
    assert ("rm", "-f", f"{added}-egress") in calls
    assert ("rm", "-f", "sca-egress") not in calls
    assert provider.egress_allowlist == ["pypi.org"]
