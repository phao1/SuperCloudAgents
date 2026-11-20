from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sca.sandbox.egress import squid_domains

PLATFORM = "platform"
KNOWLEDGE = "knowledge"
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Subject = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")]
GrantType = Literal["tenant", "team", "user"]
ToolKind = Literal["remote", "builtin"]
Endpoint = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Description = Annotated[str, StringConstraints(max_length=1000)]
HeaderReferences = Annotated[dict[str, SecretName], Field(max_length=16)]

_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
_TRANSPORT_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host", "keep-alive",
    "mcp-protocol-version", "mcp-session-id", "origin", "te", "trailer",
    "transfer-encoding", "upgrade",
}


def mcp_host(url: str, *, require_https: bool = False) -> str:
    try:
        if not url or any(c.isspace() or ord(c) < 32 or c in "\\{}" for c in url):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme == "https":
            port = 443
        elif parts.scheme == "http" and not require_https:
            port = 80
        else:
            raise ValueError
        host = parts.hostname or ""
        if (parts.port not in (None, port) or parts.username is not None or parts.fragment
                or "*" in host or "." not in host.rstrip(".")):
            raise ValueError
        return squid_domains([host])[0]
    except ValueError:
        raise ValueError("MCP 地址须为可通过出口代理访问的 HTTP(S) 域名，带凭证时须使用 HTTPS") from None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: ToolName
    description: Description = ""
    kind: ToolKind = "remote"
    url: Endpoint | None = None
    headers: HeaderReferences = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_configuration(self) -> ToolDefinition:
        if self.name == "available":
            raise ValueError("available 为保留名称")
        if self.kind == "builtin":
            if self.name != KNOWLEDGE or self.url is not None or self.headers:
                raise ValueError("内置知识库工具使用名称 knowledge")
            return self
        if self.name == KNOWLEDGE or self.url is None:
            raise ValueError("远程工具须提供 URL，knowledge 为内置工具名称")
        mcp_host(self.url, require_https=bool(self.headers))
        names = {name.lower() for name in self.headers}
        if (len(names) != len(self.headers) or names & _TRANSPORT_HEADERS
                or any(name.startswith("proxy-") or not _HEADER.fullmatch(name) for name in names)):
            raise ValueError("MCP 请求头无效或由传输层管理")
        return self


class ToolPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    description: Description | None = None
    kind: ToolKind | None = None
    url: Endpoint | None = None
    headers: HeaderReferences | None = None
    enabled: bool | None = None


class ToolRecord(ToolDefinition):
    tenant_id: str
    version: int
    created_by: str
    created_at: datetime


class AvailableTool(BaseModel):
    name: str
    description: str
    kind: ToolKind
    tenant_id: str
    version: int


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: GrantType
    subject: Subject
    tenant_id: Subject | None = None


class GrantRecord(GrantInput):
    tenant_id: Subject
    tool_tenant_id: str
    tool_name: str
    granted_by: str
