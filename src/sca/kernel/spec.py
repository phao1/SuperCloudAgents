"""Common fields for every scenario's specification."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from sca.intents.base import MAX_SOURCES, IntentSource
from sca.kernel.prompts import EvaluationSettings, validate_prompt_version


class CommonSpec(BaseModel):
    labels: dict[
        Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")],
        Annotated[str, Field(max_length=256)],
    ] = Field(default_factory=dict, max_length=16)

    intents: list[IntentSource] = Field(default_factory=list, max_length=MAX_SOURCES)

    acceptance_files: UUID | None = None
    acceptance_command: str | None = Field(default=None, min_length=1)
    auto_finish: bool = False
    agent_backend: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]*$")
    agent_version: str | None = Field(default=None, pattern=r"^2\.3\.0-sca\.(0|[1-9][0-9]*)$")
    prompt_version: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    memory_enabled: bool | None = None
    eval_run_id: str | None = None
    # Admission class (src/sca/admission); set by the API or the eval runner.
    priority: Literal["interactive", "eval", "batch"] = "interactive"

    @model_validator(mode="after")
    def acceptance_pair(self) -> "CommonSpec":
        if bool(self.acceptance_files) != bool(self.acceptance_command):
            raise ValueError("acceptance_files and acceptance_command must be supplied together")
        return self

    @model_validator(mode="after")
    def prompt_selection(self) -> "CommonSpec":
        kind = "analysis" if "question" in type(self).model_fields else "coding"
        self.prompt_version = self.prompt_version or EvaluationSettings().default_prompt_version
        validate_prompt_version(kind, self.prompt_version)
        return self

    def model_dump(self, **kwargs) -> dict:
        return super().model_dump(**{"mode": "json", **kwargs})
