from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sca.tools.models import Subject, ToolName


class ToolSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_")

    tool_teams: dict[Subject, dict[Subject, list[ToolName]]] = Field(default_factory=dict)
    knowledge_mcp_url: str | None = None
    knowledge_mcp_signing_key: SecretStr | None = None

    def teams_for(self, tenant_id: str, subject: str | None) -> list[str]:
        return self.tool_teams.get(tenant_id, {}).get(subject, []) if subject else []


@lru_cache
def get_tool_settings() -> ToolSettings:
    return ToolSettings()
