"""Common fields for every scenario's specification."""

from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from sca.intents.base import MAX_SOURCES, IntentSource


class CommonSpec(BaseModel):
    intents: list[IntentSource] = Field(default_factory=list, max_length=MAX_SOURCES)

    acceptance_files: UUID | None = None
    acceptance_command: str | None = Field(default=None, min_length=1)
    auto_finish: bool = False
    agent_backend: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]*$")
    prompt_version: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    memory_enabled: bool | None = None
    eval_run_id: str | None = None

    @model_validator(mode="after")
    def acceptance_pair(self) -> "CommonSpec":
        if bool(self.acceptance_files) != bool(self.acceptance_command):
            raise ValueError("acceptance_files and acceptance_command must be supplied together")
        return self

    def model_dump(self, **kwargs) -> dict:
        return super().model_dump(**{"mode": "json", **kwargs})
