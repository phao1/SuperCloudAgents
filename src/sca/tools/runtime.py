import asyncio
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import hvac
from pydantic import BaseModel, Field, SecretStr

from sca.core.errors import InvalidTaskError
from sca.core.interfaces import SecretProvider
from sca.core.models import AgentContext
from sca.db.repo import Store
from sca.secrets import OpenBaoSecretProvider
from sca.tools.models import ToolRecord, mcp_host
from sca.tools.settings import ToolSettings
from sca.tools.tokens import issue_knowledge_token, knowledge_url


class McpServer(BaseModel):
    url: str
    headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)


class McpAgentContext(AgentContext):
    mcp_servers: dict[str, McpServer] = Field(repr=False)


@dataclass(frozen=True)
class ToolAttachment:
    tool: ToolRecord
    url: str

    @property
    def host(self) -> str:
        return mcp_host(self.url)


async def plan_attachments(
    store: Store, settings: ToolSettings, task_id: UUID, names: list[str]
) -> list[ToolAttachment]:
    actor = await store.task_submitter(task_id)
    if not actor or not store.tenant_id:
        raise InvalidTaskError("无法识别工具任务的提交者")
    teams = settings.teams_for(store.tenant_id, actor)
    available = {row["name"]: ToolRecord.model_validate(row)
                 for row in await store.available_tools(actor, teams, names=names)}
    attachments = []
    for name in dict.fromkeys(names):
        tool = available.get(name)
        if tool is None:
            raise InvalidTaskError(f"工具不可用或未授权：{name}")
        url = knowledge_url(settings) if tool.kind == "builtin" else tool.url
        assert url is not None
        attachments.append(ToolAttachment(tool, url))
    return attachments


async def _read_header(provider: SecretProvider, tool: ToolRecord, name: str) -> SecretStr:
    if not isinstance(provider, OpenBaoSecretProvider):
        raise InvalidTaskError(f"工具 {tool.name} 的请求头凭证需要 OpenBao")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", tool.tenant_id) or tool.tenant_id in {".", ".."}:
        raise InvalidTaskError(f"工具 {tool.name} 的凭证命名空间无效")
    # Tool credentials use the tool owner's path, including for grants of platform tools.
    path = f"{provider.prefix}/{tool.tenant_id}/tools/{tool.name}/{name}"
    try:
        response = await asyncio.to_thread(
            provider.client.secrets.kv.v2.read_secret_version,
            path=path,
            mount_point=provider.mount,
            raise_on_deleted_version=True,
        )
        value = response["data"]["data"]["value"]
        if (not isinstance(value, str) or not value.strip() or len(value) > 8192
                or any(not 32 <= ord(c) <= 126 for c in value)
                or "{env:" in value or "{file:" in value):
            raise ValueError
    except (hvac.exceptions.InvalidPath, KeyError, TypeError, ValueError):
        raise InvalidTaskError(f"工具 {tool.name} 的请求头凭证缺失或无效") from None
    except Exception:  # noqa: BLE001 - provider errors can include secret response bodies
        raise InvalidTaskError(f"工具 {tool.name} 的请求头凭证读取失败") from None
    return SecretStr(value)


async def resolve_servers(
    tools: list[ToolAttachment], provider: SecretProvider, settings: ToolSettings,
    task_id: UUID, tenant_id: str, ttl_s: int,
) -> dict[str, McpServer]:
    servers = {}
    for attachment in tools:
        tool = attachment.tool
        if tool.kind == "builtin":
            token = issue_knowledge_token(settings, task_id, tenant_id, ttl_s)
            headers = {"Authorization": SecretStr(f"Bearer {token}")}
        else:
            values = {ref: await _read_header(provider, tool, ref)
                      for ref in dict.fromkeys(tool.headers.values())}
            headers = {header: values[ref] for header, ref in tool.headers.items()}
        servers[tool.name] = McpServer(url=attachment.url, headers=headers)
    return servers


def redact_mcp_output(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
    elif isinstance(value, dict):
        value = {redact_mcp_output(key, secrets): redact_mcp_output(item, secrets)
                 for key, item in value.items()}
    elif isinstance(value, list):
        value = [redact_mcp_output(item, secrets) for item in value]
    return value
