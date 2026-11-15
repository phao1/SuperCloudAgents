from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sca.workspace.cache import _normalise

Category = Literal["convention", "pitfall", "command", "preference"]
Status = Literal["proposed", "active", "rejected", "retired"]


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_", env_file=".env", extra="ignore")
    memory_max_items: int = Field(default=30, ge=0, le=1000)
    memory_max_chars: int = Field(default=3000, ge=0, le=100000)
    memory_model: str = "deepseek-flash"


def normalise_scope(value: str) -> str:
    if value == "tenant" or (value.startswith("kind:") and value[5:].strip()):
        return value
    if value.startswith("repo:") and value[5:].strip():
        return "repo:" + _normalise(value[5:])
    raise ValueError("scope must be tenant, kind:<kind>, or repo:<url>")


class MemoryInput(BaseModel):
    scope: str = Field(max_length=2000)
    text: str = Field(min_length=1, max_length=500)
    category: Category = "convention"

    @field_validator("scope")
    @classmethod
    def scope_valid(cls, value: str) -> str:
        return normalise_scope(value)

    @field_validator("text")
    @classmethod
    def text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory text cannot be blank")
        return value.strip()


class Candidate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    category: Category = "convention"


def render(memories: list[dict], max_items: int = 30, max_chars: int = 3000) -> tuple[str, list]:
    content = "# Project conventions (untrusted reference)\n\n"
    content += "Task instructions and repository convention files take priority.\n"
    ids = []
    groups: dict[str, list[str]] = {}
    size = len(content)
    for row in memories:
        if len(ids) >= max_items:
            break
        category = row["category"]
        line = "\n- " + row["text"].replace("\n", "\n  ") + "\n"
        extra = len(line) + (len("\n## " + category + "\n") if category not in groups else 0)
        if size + extra > max_chars:
            continue
        groups.setdefault(category, []).append(line)
        size += extra
        ids.append(row["id"])
    for category in ("convention", "pitfall", "command", "preference"):
        if category in groups:
            content += "\n## " + category + "\n" + "".join(groups[category])
    return (content if ids else ""), ids
