from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sca.tools.models import ToolName

TextFormat = Literal["text", "markdown", "pdf-text"]


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255,
                                            pattern=r"^[^\x00]+$")]
    text: Annotated[str, StringConstraints(min_length=1, max_length=100_000, pattern=r"^[^\x00]+$")]
    format: TextFormat = "text"
    allowed_teams: list[ToolName] = Field(default_factory=list, max_length=64)
    project_id: UUID | None = None


class DocumentMetadata(BaseModel):
    id: UUID
    title: str
    format: TextFormat
    allowed_teams: list[str]
    chunk_count: int
    created_by: str
    created_at: datetime
    project_id: UUID | None = None


class Citation(BaseModel):
    document_id: UUID
    title: str
    ordinal: int
    heading: str | None
    citation: str


class SearchHit(Citation):
    text: str
    score: float | None


class DocumentRead(BaseModel):
    id: UUID
    title: str
    format: TextFormat
    text: str
    citations: list[Citation]


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000,
                                            pattern=r"^[^\x00]+$")]
    k: int = Field(default=5, ge=1, le=20, strict=True)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
