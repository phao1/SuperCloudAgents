"""Compare full clones with private reflink copies; always remove generated data."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from sca.core.errors import WorkspaceError
from sca.workspace.git import GitWorkspace
from sca.workspace.layers import LayerStore, command, disk_bytes, permissions


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(".data/opencode/opencode.git"))
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("-n", type=int, default=2)
    parser.add_argument("--overlay", action="store_true", help="Include Docker overlay")
    args = parser.parse_args()
    if not 1 <= args.n <= 10:
        parser.error("n must be between 1 and 10")
    repo = args.repo.resolve()
    # Use the same filesystem as the real tasks, while deleting one method before the next.
    with tempfile.TemporaryDirectory(prefix="workspace-bench-", dir=repo.parent) as temporary:
        root = Path(temporary)
        probe = root / "probe"
        probe.write_bytes(os.urandom(4096))
        try:
            await command("cp", "--reflink=always", str(probe), str(root / "reflink"))
            reflink = True
        except WorkspaceError:
            reflink = False
        for name in ("lower", "upper", "work", "merged"):
            (root / name).mkdir()
        try:
            await command("unshare", "-Urnm", "mount", "-t", "overlay", "overlay", "-o",
                          f"lowerdir={root / 'lower'},upperdir={root / 'upper'},"
                          f"workdir={root / 'work'}", str(root / "merged"))
            private_namespace_overlay = True
        except (WorkspaceError, FileNotFoundError):
            private_namespace_overlay = False
        print(json.dumps({"private_namespace_overlay": private_namespace_overlay,
                          "host_visible_overlay": False, "reflink": reflink, "fuse_overlayfs": shutil.which("fuse-overlayfs"),
                          "dev_fuse": Path("/dev/fuse").exists()}), flush=True)
        for method in ("old", "new", *(["overlay"] if args.overlay else [])):
            layers = LayerStore(root / "layers") if method in {"new", "overlay"} else None
            for index in range(args.n):
                task = root / f"{root.name}-{index}"
                from sca.workspace.overlay import OverlayWorkspace

                cls = OverlayWorkspace if method == "overlay" else GitWorkspace
                ws = cls(task, f"sca/bench-{index}", layers=layers)
                started = time.monotonic()
                try:
                    await ws.prepare(str(repo), args.ref)
                except BaseException:
                    if method == "overlay" and (task / "workspace.overlay").exists():
                        await ws.cleanup()
                    raise
                seconds = round(time.monotonic() - started, 3)
                try:
                    # overlay/work/work is owned by root (the kernel creates it), so only the upper
                    # layer is measurable without privileges; it is what the task adds on disk.
                    own = await disk_bytes(getattr(ws, "upper", ws.workspace))
                    total = own if method == "overlay" else await disk_bytes(task)
                    print(json.dumps({"method": method, "index": index, "seconds": seconds,
                                      "workspace_bytes": own, "task_bytes": total}), flush=True)
                finally:
                    if method == "overlay":
                        await ws.cleanup()
                    shutil.rmtree(task)
            if layers:
                print(json.dumps({"layer_bytes": await layers.prune(24)}), flush=True)
                await asyncio.to_thread(permissions, layers.root, True)
                shutil.rmtree(layers.root)


if __name__ == "__main__":
    asyncio.run(main())
