from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from sca.agents.opencode import validate_version
    from sca.config import Settings


class Runner(Protocol):
    async def __call__(self, argv: list[str], log: Path) -> str: ...


async def run_command(argv: list[str], log: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    output = bytearray()
    try:
        async with asyncio.timeout(900):
            assert process.stdout is not None
            with log.open("ab", buffering=0) as stream:
                while chunk := await process.stdout.read(8192):
                    stream.write(chunk)
                    output.extend(chunk)
                    if len(output) > 8192:
                        del output[:-8192]
            await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise

    if process.returncode:
        raise RuntimeError(f"Build command failed ({process.returncode}); see build log")
    return output.decode(errors="replace").strip()


def build_command(
    settings: Settings, source: Path, version: str, *, mount_dependencies: bool = True
) -> list[str]:
    validate_version(version)
    return [
        settings.docker_bin,
        "run",
        "--rm",
        "--network=none",
        f"--memory={settings.opencode_build_memory}",
        f"--memory-swap={settings.opencode_build_memory}",
        "--cpus=2",
        "-u",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        "OPENCODE_CHANNEL=sca",
        "-e",
        f"OPENCODE_VERSION={version}",
        "-v",
        f"{source.resolve()}:/src",
        *(["-v", (f"{Path(settings.opencode_repo_url).resolve()}:"
                   f"{Path(settings.opencode_repo_url).resolve()}:ro")]
          if Path(settings.opencode_repo_url).is_dir() else []),
        *(dependency_args(settings, source) if mount_dependencies else []),
        "-w",
        "/src/packages/cli",
        settings.opencode_build_image,
        "bun",
        "run",
        "script/build.ts",
        "--single",
        "--skip-install",
        "--skip-web-ui",
    ]


def dependency_paths(settings: Settings) -> list[Path]:
    cache = settings.opencode_src_cache.resolve()
    return [cache / "node_modules", *sorted(cache.glob("packages/*/node_modules"))]


def dependency_args(settings: Settings, source: Path) -> list[str]:
    cache = settings.opencode_src_cache.resolve()
    args = []
    for modules in dependency_paths(settings):
        target = source / modules.relative_to(cache)
        if not target.resolve().is_relative_to(source.resolve()):
            raise RuntimeError("Dependency destination escapes build checkout")
        if modules.is_symlink() or not modules.resolve().is_relative_to(cache):
            raise RuntimeError("Dependency cache escapes trusted source")
        args.extend(["-v", f"{modules}:/src/{modules.relative_to(cache)}:ro"])
    return args


def records_dir(settings: Settings) -> Path:
    return settings.opencode_versions_dir / "builds"


def write_record(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    temporary.replace(path)


def reserve_build(settings: Settings, build_id: str, tenant_id: str, **fields) -> dict:
    root = settings.opencode_versions_dir
    root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        version = f"2.3.0-sca.{n}"
        try:
            (root / version).mkdir()
            break
        except FileExistsError:
            n += 1
    records_dir(settings).mkdir(exist_ok=True)
    record = dict(
        id=build_id,
        tenant_id=tenant_id,
        version=version,
        status="queued",
        commit=None,
        created_at=datetime.now(UTC).isoformat(),
        **fields,
    )
    write_record(records_dir(settings) / f"{build_id}.json", record)
    return record


async def perform_build(settings: Settings, record: dict, runner: Runner = run_command) -> dict:
    lock_path = records_dir(settings) / "build.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            record = {**record, "status": "failed", "error": "已有构建正在运行，请稍后重试"}
            write_record(records_dir(settings) / f"{record['id']}.json", record)
            raise RuntimeError("Another OpenCode build is running") from exc
        return await _perform_build(settings, record, runner)


async def _perform_build(settings: Settings, record: dict, runner: Runner) -> dict:
    build_id, version = record["id"], validate_version(record["version"])
    root = settings.data_dir.resolve() / "opencode" / "build-work" / build_id
    root.mkdir(parents=True, exist_ok=True)
    log = records_dir(settings) / f"{build_id}.log"
    path = records_dir(settings) / f"{build_id}.json"
    meta_path = settings.opencode_versions_dir / version / "meta.json"
    if meta_path.exists():
        previous = json.loads(path.read_text())
        previous["status"] = "succeeded"
        previous["commit"] = json.loads(meta_path.read_text())["commit"]
        write_record(path, previous)
        await asyncio.to_thread(shutil.rmtree, root)
        return previous
    started = time.monotonic()
    record = {**record, "status": "running"}
    write_record(path, record)
    source = root / "src"
    try:
        # Git runs only before the checkout has been exposed to the build container.
        if source.exists():
            shutil.rmtree(source)
        await runner(
            ["git", "clone", "--quiet", "--shared", "--", settings.opencode_repo_url, str(source)], log
        )
        await runner(["git", "-C", str(source), "fetch", "--quiet", "origin", record["ref"]], log)
        await runner(["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"], log)
        commit = await runner(["git", "-C", str(source), "rev-parse", "HEAD"], log)
        cache = settings.opencode_src_cache
        if not (cache / "node_modules").is_dir():
            raise RuntimeError("OpenCode dependency cache missing; run setup before building")
        await runner(build_command(settings, source, version), log)
        destination = settings.opencode_versions_dir / version
        binary = source / f"packages/cli/dist/cli-linux-{settings.container_arch}/bin/opencode"
        if (
            not binary.is_file()
            or binary.is_symlink()
            or not binary.resolve().is_relative_to(source.resolve())
            or binary.stat().st_size > 512 * 1024 * 1024
        ):
            raise RuntimeError("Invalid build artifact")
        shutil.copyfile(binary, destination / "opencode")
        (destination / "opencode").chmod(0o755)
        meta = {
            "version": version,
            "source_repo": settings.opencode_repo_url,
            "source_ref": record["ref"],
            "commit": commit,
            "base_version": record.get("base_version", settings.opencode_default_version),
            "built_at": datetime.now(UTC).isoformat(),
            "build_seconds": round(time.monotonic() - started, 3),
            "notes": record.get("notes"),
            "task_id": record.get("task_id"),
            "labels": record.get("labels", {}),
        }
        write_record(destination / "meta.json", meta)
        record.update(status="succeeded", commit=commit)
    except Exception:
        record.update(status="failed", error="构建失败，请查看构建日志")
        write_record(path, record)
        raise
    write_record(path, record)
    await asyncio.to_thread(shutil.rmtree, root)
    return record


class OpenCodeBuildActivities:
    def __init__(self, settings: Settings):
        self.settings = settings

    @activity.defn(name="opencode_build")
    async def build(self, record: dict) -> dict:
        return await perform_build(self.settings, record)


@workflow.defn
class OpenCodeBuildWorkflow:
    @workflow.run
    async def run(self, record: dict) -> dict:
        return await workflow.execute_activity(
            "opencode_build",
            record,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
