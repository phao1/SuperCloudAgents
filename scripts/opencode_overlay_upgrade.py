"""Add runtime overlays to an existing trusted OpenCode bare repository."""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from pathlib import Path

from sca.agents.opencode_build import run_command
from sca.config import get_settings


async def upgrade() -> None:
    settings = get_settings()
    bare = Path(settings.opencode_repo_url).resolve()
    root = settings.data_dir.resolve() / "opencode"
    root.mkdir(parents=True, exist_ok=True)
    log = root / "overlay-upgrade.log"
    patch = Path(__file__).resolve().parents[1] / "deploy/opencode/sca-overlay.patch"
    with tempfile.TemporaryDirectory(prefix="sca-overlay-") as temporary:
        work = Path(temporary) / "src"
        await run_command(["git", "clone", "--branch", "main", str(bare), str(work)], log)
        git = ["git", "-C", str(work)]
        # Check content rather than a commit title, so a reverted patch is applied again.
        try:
            await run_command([*git, "apply", "--reverse", "--check", str(patch)], log)
        except RuntimeError:
            await run_command([*git, "apply", "--check", str(patch)], log)
            await run_command([*git, "apply", str(patch)], log)
            await run_command([*git, "add", "packages/core"], log)
            await run_command(
                [*git, "-c", "user.name=SCA Setup", "-c", "user.email=setup@sca.local",
                 "commit", "-q", "-m", "Allow runtime prompt and tool overlays"], log,
            )
            # A concurrent update fails the push rather than replacing main.
            await run_command([*git, "push", str(bare), "HEAD:refs/heads/main"], log)
            print("Runtime overlay commit added.")
        else:
            print("Runtime overlay patch is already present.")
        commit = (await run_command([*git, "rev-parse", "HEAD"], log)).strip()
    body = json.dumps({"ref": commit, "notes": "Runtime prompt and tool overlays"})
    print("Register and build the overlay-capable version (not executed):")
    print('curl -X POST "${SCA_API_URL:-http://localhost:8000}/v1/agents/opencode/builds" '
          '-H "Authorization: Bearer ${SCA_API_TOKEN}" '
          '-H "Content-Type: application/json" --data ' + shlex.quote(body))


if __name__ == "__main__":
    asyncio.run(upgrade())
