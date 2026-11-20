"""Intent contracts and bounded, local-only source reading."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 16
TICKET_TYPES = frozenset({"jira", "linear", "github_issue"})
REMOTE_TYPES = TICKET_TYPES | {"figma"}


class IntentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,30}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    path: str | None = None
    file_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9-]{1,80}$")
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reference(self) -> IntentSource:
        if len(json.dumps(self.params).encode()) > 32768:
            raise ValueError("intent parameters exceed size limit")
        if self.type in REMOTE_TYPES:
            if self.path is not None or self.file_id is not None:
                raise ValueError("remote intents use params without path or file_id")
        elif (self.path is None) == (self.file_id is None):
            raise ValueError("provide exactly one of path or file_id")
        return self


class Check(BaseModel):
    name: str
    command: str
    artifacts: list[str] = Field(default_factory=list)
    needs_base_sha: bool = False


class IntentBundle(BaseModel):
    context_files: dict[str, bytes] = Field(default_factory=dict)
    prompt_notes: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    setup: list[str] = Field(default_factory=list)


class IntentAdapter(Protocol):
    type: str

    def compile(self, source: IntentSource, workspace: Path) -> IntentBundle: ...


_registry: dict[str, IntentAdapter] = {}


def register(adapter: IntentAdapter) -> None:
    if adapter.type in _registry:
        raise ValueError(f"intent adapter already registered: {adapter.type}")
    _registry[adapter.type] = adapter


def get(name: str) -> IntentAdapter:
    if name not in _registry:
        raise ValueError(f"unknown intent adapter: {name}")
    return _registry[name]


def read_local(workspace: Path, name: str) -> bytes:
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or any(p in {"..", "."} for p in parts):
        raise ValueError("source must be a relative file path")
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("source must be a regular file")
            data = stream.read(MAX_SOURCE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("intent source exceeds size limit")
    return data


def source_bytes(source: IntentSource, workspace: Path) -> bytes:
    if source.path is None:
        raise ValueError("uploaded sources must be staged before compilation")
    return read_local(workspace, source.path)
