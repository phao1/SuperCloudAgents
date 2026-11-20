from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import jwt

from sca.core.errors import AuthError, InvalidTaskError
from sca.tools.models import mcp_host
from sca.tools.settings import ToolSettings

PATH = "/v1/mcp/knowledge"
_ISSUER = "sca"
_AUDIENCE = "sca:knowledge:mcp"


def signing_key(settings: ToolSettings) -> str:
    secret = settings.knowledge_mcp_signing_key
    key = secret.get_secret_value() if secret else ""
    if len(key.encode()) < 32:
        raise InvalidTaskError("知识库 MCP 须配置至少 32 字节的签名密钥")
    return key


def knowledge_url(settings: ToolSettings) -> str:
    url = settings.knowledge_mcp_url or ""
    try:
        mcp_host(url, require_https=True)
        parts = urlsplit(url)
        if parts.path != PATH or parts.query:
            raise ValueError
    except ValueError:
        raise InvalidTaskError(f"请将 SCA_KNOWLEDGE_MCP_URL 设为 HTTPS 服务的 {PATH} 地址") from None
    signing_key(settings)
    return url


def issue_knowledge_token(settings: ToolSettings, task_id: UUID, tenant_id: str, ttl_s: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": str(task_id),
            "tenant": tenant_id,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_s),
        },
        signing_key(settings),
        algorithm="HS256",
    )


def verify_knowledge_token(settings: ToolSettings, token: str) -> tuple[UUID, str]:
    try:
        claims = jwt.decode(
            token,
            signing_key(settings),
            algorithms=["HS256"],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            options={"require": ["iss", "aud", "sub", "tenant", "iat", "exp"]},
        )
        tenant = claims["tenant"]
        if not isinstance(tenant, str) or not tenant:
            raise ValueError
        return UUID(claims["sub"]), tenant
    except (jwt.PyJWTError, TypeError, ValueError, InvalidTaskError):
        raise AuthError("知识库 MCP 令牌无效或已过期") from None
