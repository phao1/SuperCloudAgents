"""Analysis task specification."""

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from sca.kernel.spec import CommonSpec


class AnalysisSpec(CommonSpec):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    inputs: list[UUID] = Field(min_length=1, max_length=32)
    report_format: Literal["markdown", "html"] = "markdown"
    checks: list[str] = Field(default_factory=list, max_length=16)
    setup_command: str | None = None
    image: str | None = "sca-sandbox-analysis:latest"
    model: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    turn_timeout_s: int = Field(default=1800, ge=30, le=21600)
    max_fix_attempts: int = Field(default=2, ge=0, le=10)

    def model_dump(self, **kwargs) -> dict:
        return super().model_dump(**{"mode": "json", **kwargs})
