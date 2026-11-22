"""The built-in OpenCode 2.3 system prompt used by the demo's DeepSeek models."""

import asyncio
import contextlib
import hashlib
import json
import re
from pathlib import Path

from sca.agents.opencode import validate_version
from sca.config import Settings

PROMPT_PATH = "packages/core/src/session/runner/prompt/system.txt"
PROMPT_SUITE = "opencode-prompt"
PROMPT_TARGET = "system_prompt"
TOOL_GUIDANCE = "${OPENCODE_TOOL_GUIDANCE}"
MAX_PROMPT_BYTES = 24 * 1024


def is_commit(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def version_commit(settings: Settings, version: str) -> str:
    directory = settings.opencode_versions_dir / validate_version(version)
    metadata = directory / "meta.json"
    binary = directory / "opencode"
    if any(path.is_symlink() for path in (directory, metadata, binary)) or not binary.is_file():
        raise ValueError("OpenCode version is not installed")
    entry = json.loads(metadata.read_text())
    if (
        not isinstance(entry, dict)
        or entry.get("version") != version
        or entry.get("source_repo") != settings.opencode_repo_url
        or not is_commit(entry.get("commit"))
    ):
        raise ValueError("OpenCode version has no matching source commit")
    return entry["commit"]


def prompt_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PROMPT_BYTES:
        raise ValueError("Invalid OpenCode system prompt file")
    data = path.read_bytes()
    value = data.decode("utf-8")
    if value.count(TOOL_GUIDANCE) != 1 or not value.replace(TOOL_GUIDANCE, "").strip():
        raise ValueError("The system prompt must keep the OpenCode tool guidance placeholder")
    return hashlib.sha256(data).hexdigest()


async def read_source_prompt(settings: Settings, commit: str) -> str:
    """Read the bounded prompt blob at an immutable commit in the configured source repo."""
    if not is_commit(commit):
        raise ValueError("Invalid prompt source commit")
    ref = f"{commit}:{PROMPT_PATH}"

    async def git(*args: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", settings.opencode_repo_url, "cat-file", *args, ref,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
        except (TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError("OpenCode prompt blob is unavailable")
        return stdout

    if int(await git("-s")) > MAX_PROMPT_BYTES:
        raise ValueError("OpenCode system prompt exceeds the supported size")
    return (await git("blob")).decode("utf-8")
