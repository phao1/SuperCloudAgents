"""Prepare trusted demo sources; Docker execution requires --execute-docker."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from sca.agents.opencode_build import build_command, perform_build, reserve_build, run_command
from sca.config import get_settings


async def setup(execute_docker: bool) -> None:
    settings = get_settings()
    root = settings.data_dir.resolve() / "opencode"
    root.mkdir(parents=True, exist_ok=True)
    log = root / "setup.log"
    source = settings.opencode_src_cache
    if not source.exists():
        await run_command(
            [
                "git",
                "clone",
                "--branch",
                "v2.3.0",
                "--single-branch",
                "https://github.com/anomalyco/opencode.git",
                str(source),
            ],
            log,
        )
    bare = Path(settings.opencode_repo_url)
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        # A fresh two-commit history (v2.3.0 snapshot + the planted defect): the platform's repo
        # cache refuses shallow references, and the full upstream history is ~700 MB.
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            work = Path(temporary) / "src"
            work.mkdir()
            await run_command(["git", "-C", str(work), "init", "-q", "-b", "main"], log)
            archive = Path(temporary) / "v2.3.0.tar"
            await run_command(
                ["git", "-C", str(source), "archive", "-o", str(archive), "v2.3.0"], log
            )
            await run_command(["tar", "-xf", str(archive), "-C", str(work)], log)
            identity = ["-c", "user.name=SCA Setup", "-c", "user.email=setup@sca.local"]
            await run_command(["git", "-C", str(work), "add", "-A"], log)
            await run_command(
                ["git", "-C", str(work), *identity, "commit", "-q", "-m", "OpenCode v2.3.0"], log
            )
            patch = Path("deploy/opencode/planted-bug.patch").resolve()
            await run_command(["git", "-C", str(work), "apply", str(patch)], log)
            await run_command(
                ["git", "-C", str(work), "add", "packages/core/src/file-write.ts"], log
            )
            await run_command(
                [
                    "git",
                    "-C",
                    str(work),
                    "-c",
                    "user.name=SCA Setup",
                    "-c",
                    "user.email=setup@sca.local",
                    "commit",
                    "-m",
                    "Normalize text written by file tools",
                ],
                log,
            )
            await run_command(["git", "init", "-q", "--bare", "-b", "main", str(bare)], log)
            await run_command(["git", "-C", str(work), "push", "-q", str(bare), "main"], log)
        await run_command(
            ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], log
        )
    context = root / "image-context"
    image = [
        settings.docker_bin,
        "build",
        "-f",
        str(Path("deploy/opencode/sandbox.Dockerfile").resolve()),
        "-t",
        settings.opencode_sandbox_image,
        str(context),
    ]
    install = build_command(settings, source, "2.3.0-sca.1", mount_dependencies=False)
    install[install.index("--network=none")] = "--network=bridge"
    install[install.index("-w") + 1] = "/src"
    install = install[: install.index("bun")] + ["bun", "install", "--frozen-lockfile"]
    if not execute_docker:
        if not (source / "node_modules").is_dir():
            print(shlex.join(install))
        print(shlex.join(image))
        print(shlex.join(build_command(settings, root / "build-work/BUILD_ID/src", "2.3.0-sca.1")))
        print("Docker execution deferred. Re-run with --execute-docker after reviewing commands.")
        return
    if not (source / "node_modules").is_dir():
        await run_command(install, log)
    context.mkdir(exist_ok=True)
    shutil.copyfile(Path(f".data/tools/bun-linux-{settings.container_arch}/bun"), context / "bun")
    legacy_cache = context / "cache"
    if legacy_cache.exists():
        shutil.rmtree(legacy_cache)
    await run_command(image, log)
    meta = settings.opencode_versions_dir / "2.3.0-sca.1/meta.json"
    if not meta.exists():
        import json

        records = settings.opencode_versions_dir / "builds"
        previous = [json.loads(p.read_text()) for p in records.glob("*.json")]
        record = next((r for r in previous if r["version"] == "2.3.0-sca.1"), None)
        if record is None:
            record = reserve_build(
                settings, str(uuid4()), "default", ref="main", task_id=None, notes="Demo baseline"
            )
        if record["version"] != "2.3.0-sca.1":
            raise RuntimeError("Baseline version is already reserved; inspect build records")
        await perform_build(settings, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-docker", action="store_true")
    asyncio.run(setup(parser.parse_args().execute_docker))
