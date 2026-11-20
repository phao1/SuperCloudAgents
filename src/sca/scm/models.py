from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sca.kinds.coding.spec import PullRequestSpec
from sca.scm.repository import ProviderName, normalize_host, normalize_repo_name


class IssueDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verify_command: str | None = None
    verify_commands: list[str] = Field(default_factory=list, max_length=16)
    setup_command: str | None = None
    image: str | None = None
    base_ref: str | None = Field(default=None, min_length=1)
    trigger_labels: list[str] = Field(default_factory=list)
    pull_request: PullRequestSpec = Field(default_factory=PullRequestSpec)


class IntegrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    host: str = Field(max_length=260)
    repo_full_name: str = Field(max_length=260)
    webhook_secret_ref: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    default_spec: IssueDefaults = Field(default_factory=IssueDefaults)
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def host_key(cls, value: str) -> str:
        return normalize_host(value)

    @field_validator("repo_full_name")
    @classmethod
    def repo_key(cls, value: str) -> str:
        return normalize_repo_name(value)


class Integration(IntegrationInput):
    id: UUID
    tenant_id: str
    created_at: datetime
    updated_at: datetime


class Issue(BaseModel):
    provider: ProviderName
    number: int = Field(gt=0, strict=True)
    url: str


class PullRequest(BaseModel):
    provider: ProviderName
    number: int = Field(gt=0, strict=True)
    url: str
    state: Literal["open", "closed", "merged"]
