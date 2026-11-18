"""Measure prepared-tree forks with Docker; no agent, install, or build is implicit."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from sca.config import Settings
from sca.core.models import ExecExit, Mount, SandboxSpec
from sca.eval.suites import load_suite
from sca.sandbox.docker import DockerSandboxProvider
from sca.workspace.git import GitWorkspace
from sca.workspace.layers import command, disk_bytes, permissions
from sca.workspace.overlay import OverlayWorkspace
from sca.workspace.snapshots import SnapshotStore, preparation_key
from sca.workspace.transport import MountTransport

# Compare files rather than timestamps or task-specific Git refs.
PROBE = '''import hashlib, os
from pathlib import Path
h = hashlib.sha256()
for directory, dirs, files in os.walk('.'):
    dirs[:] = sorted(d for d in dirs if d not in ('.git', '.sca'))
    for name in sorted(files):
        p = Path(directory) / name
        h.update(str(p).encode() + b'\\0')
        if p.is_symlink():
            h.update(os.readlink(p).encode())
        else:
            with p.open('rb') as f:
                while b := f.read(1048576): h.update(b)
print(h.hexdigest())
'''


async def measure(args: argparse.Namespace, root: Path, name: str, repo: str,
                  ref: str, setup: str, image: str) -> dict:
    settings = Settings(sandbox_driver="docker")
    provider = DockerSandboxProvider(settings.docker_bin,
                                     egress_allowlist=settings.egress_allowlist())
    snapshots = SnapshotStore(root / "snapshots")
    samples = []
    baseline = None
    prepared_seconds = 0.0
    key = None
    metadata = None
    for index in range(args.forks + 1):
        task = root / "tasks" / str(uuid4())
        ws = (OverlayWorkspace(task, f"sca/bench-{index}", image=image) if args.overlay
              else GitWorkspace(task, f"sca/bench-{index}"))
        sandbox_id = None
        started = time.monotonic()
        try:
            prepared = await ws.prepare(repo, ref, mirror_only=index > 0)
            if index:
                assert key is not None
                metadata = await snapshots.fork(
                    key, ws.overlay if args.overlay else ws.workspace, overlay=args.overlay)
                assert metadata is not None
                if args.overlay:
                    ws.overlay.mkdir(parents=True, exist_ok=True)
                    for part in ("upper", "work"):
                        (ws.overlay / part).mkdir()
                    (task / "workspace.overlay").write_text(json.dumps({
                        "volume": ws.volume, "lower": str(snapshots.root / metadata["id"])}))
                    await ws.ensure_volume()
                (task / "workspace.ready").write_text(prepared.base_sha)
            mounts: list[Mount] = MountTransport(provider).mounts(task)
            sandbox_id = await provider.create(f"sca-fork-bench-{task.name}", SandboxSpec(
                image=image, mounts=mounts, network="restricted", memory_mb=512, cpus=1))
            argv = (["bash", "-lc", setup] if not index else [
                "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "checkout", "-B", ws.branch, prepared.base_sha])
            async for event in provider.exec(sandbox_id, argv,
                                             workdir="/workspace", timeout_s=840):
                if isinstance(event, ExecExit) and event.exit_code:
                    raise RuntimeError("Benchmark preparation failed")
            elapsed = time.monotonic() - started
            output = []
            async for event in provider.exec(sandbox_id, ["python3", "-c", PROBE],
                                             workdir="/workspace", timeout_s=120):
                if isinstance(event, ExecExit):
                    if event.exit_code:
                        raise RuntimeError("Benchmark probe failed")
                else:
                    output.append(event.text)
            fingerprint = "\n".join(output)
            if index == 0:
                baseline = fingerprint
                prepared_seconds = elapsed
                key = preparation_key("benchmark", repo, prepared.base_sha, setup, image)
                metadata = await snapshots.publish(key, provider.download(sandbox_id, "/workspace/."),
                                                   base_commit=prepared.base_sha,
                                                   saved_seconds=elapsed)
            else:
                if fingerprint != baseline:
                    raise RuntimeError("Fork files differ from the prepared workspace")
                samples.append({"seconds": elapsed, "files_equal": fingerprint == baseline,
                                "workspace_bytes": await disk_bytes(
                                    ws.upper if args.overlay else ws.workspace),
                                "task_bytes": await disk_bytes(ws.mirror) + await disk_bytes(
                                    ws.upper if args.overlay else ws.workspace)})
        finally:
            if sandbox_id:
                await provider.destroy(sandbox_id)
            if args.overlay and (task / "workspace.overlay").exists():
                await ws.cleanup()
            if task.exists():
                await asyncio.to_thread(permissions, task, True)
                shutil.rmtree(task)
    return {"case": name, "overlay": args.overlay, "prepare_setup_seconds": prepared_seconds,
            "snapshot_bytes": metadata["size_bytes"] if metadata else 0, "forks": samples,
            "average_fork_seconds": sum(s["seconds"] for s in samples) / len(samples),
            "all_files_equal": all(s["files_equal"] for s in samples)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("evals/opencode-demo"))
    parser.add_argument("--heavy-repo", default=".data/opencode/opencode.git")
    parser.add_argument("--heavy-ref", default="HEAD")
    parser.add_argument("--heavy-setup", default="true",
                        help="Explicit reviewed setup; defaults to no dependency install/build")
    parser.add_argument("--image", default="sca-sandbox-opencode:latest")
    parser.add_argument("--forks", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--overlay", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fork-bench-", dir=settings.data_dir) as temporary:
        root = Path(temporary)
        try:
            suite = load_suite(args.suite)
            for case in suite["cases"]:
                source = Path(case["path"])
                repo = source / "repo.bundle"
                if not repo.exists():
                    seed = root / f"seed-{case['id']}"
                    shutil.copytree(source / "repo", seed)
                    await command("git", "-C", str(seed), "init", "-b", "main")
                    await command("git", "-C", str(seed), "add", ".")
                    await command("git", "-C", str(seed), "-c", "user.name=Benchmark",
                                  "-c", "user.email=bench@invalid", "-c", "commit.gpgsign=false",
                                  "commit", "-m", "fixture")
                    repo = seed
                spec = {**suite.get("defaults", {}), **case["spec"]}
                print(json.dumps(await measure(args, root, case["id"], str(repo.resolve()),
                    spec.get("base_ref", "main"), spec.get("setup_command") or "true",
                    spec.get("image") or args.image)), flush=True)
            print(json.dumps(await measure(args, root, "opencode-source", args.heavy_repo,
                                            args.heavy_ref, args.heavy_setup, args.image)), flush=True)
        finally:
            await asyncio.to_thread(permissions, root, True)


if __name__ == "__main__":
    asyncio.run(main())
