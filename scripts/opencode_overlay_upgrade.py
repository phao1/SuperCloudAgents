"""Add runtime and compiled overlays to an existing trusted OpenCode bare repository."""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from pathlib import Path

from sca.agents.opencode_build import run_command
from sca.config import get_settings


async def applied(git: list[str], patch: Path, log: Path) -> bool:
    try:
        await run_command([*git, "apply", "--reverse", "--check", str(patch)], log)
    except RuntimeError:
        return False
    return True


async def commit_patch(git: list[str], patch: Path, message: str, log: Path) -> None:
    await run_command([*git, "apply", str(patch)], log)
    await run_command([*git, "add", "packages/core"], log)
    await run_command(
        [*git, "-c", "user.name=SCA Setup", "-c", "user.email=setup@sca.local",
         "commit", "-q", "-m", message], log,
    )


async def upgrade() -> None:
    settings = get_settings()
    bare = Path(settings.opencode_repo_url).resolve()
    root = settings.data_dir.resolve() / "opencode"
    root.mkdir(parents=True, exist_ok=True)
    log = root / "overlay-upgrade.log"
    patches = Path(__file__).resolve().parents[1] / "deploy/opencode"
    runtime = patches / "sca-overlay.patch"
    embedded = patches / "sca-overlay-default.patch"
    with tempfile.TemporaryDirectory(prefix="sca-overlay-") as temporary:
        work = Path(temporary) / "src"
        await run_command(["git", "clone", "--branch", "main", str(bare), str(work)], log)
        git = ["git", "-C", str(work)]
        # The compiled layer edits files from the runtime layer. Check it first.
        if not await applied(git, embedded, log):
            message = "Allow compiled-in prompt and tool overlays"
            if not (work / "packages/core/src/sca-overlay.ts").is_file():
                await run_command([*git, "apply", str(runtime)], log)
                message = "Allow runtime prompt and tool overlays"
            await commit_patch(git, embedded, message, log)
            # A concurrent update fails the push rather than replacing main.
            await run_command([*git, "push", str(bare), "HEAD:refs/heads/main"], log)
            print("Compiled overlay commit added.")
        else:
            print("Compiled overlay patch is already present.")
        commit = (await run_command([*git, "rev-parse", "HEAD"], log)).strip()
    body = json.dumps({"ref": commit, "notes": "Runtime and compiled prompt and tool overlays"})
    print("Register and build the overlay-capable version (not executed):")
    print('curl -X POST "${SCA_API_URL:-http://localhost:8000}/v1/agents/opencode/builds" '
          '-H "Authorization: Bearer ${SCA_API_TOKEN}" '
          '-H "Content-Type: application/json" --data ' + shlex.quote(body))


if __name__ == "__main__":
    asyncio.run(upgrade())
