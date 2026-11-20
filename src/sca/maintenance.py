"""Independent, bounded periodic housekeeping activities."""

import asyncio
import hashlib
import logging
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from sca.archive import archive_finished
    from sca.config import Settings
    from sca.core.errors import ConflictError, NotFoundError, WorkspaceError
    from sca.core.interfaces import ModelGateway, ObjectStore, SandboxProvider
    from sca.core.models import TERMINAL_STATES, SandboxSpec
    from sca.db.repo import Store
    from sca.sandbox.k8s import K8sSandboxProvider
    from sca.workspace.cache import RepoCache
    from sca.workspace.state import TaskStateStore


@workflow.defn
class MaintenanceWorkflow:
    @workflow.run
    async def run(self) -> None:
        for name in (
            "archive_events",
            "prune_repo_cache",
            "reap_sandboxes",
            "prune_workspaces",
            "expire_gateway_keys",
            "replenish_sandbox_pool",
        ):
            try:
                await workflow.execute_activity(
                    name,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("Maintenance activity failed: %s", name)


class MaintenanceActivities:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        objects: ObjectStore,
        sandbox: SandboxProvider,
        gateway: ModelGateway | None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.objects = objects
        self.sandbox = sandbox
        self.gateway = gateway

    def all(self) -> list:
        return [
            self.archive_events,
            self.prune_repo_cache,
            self.reap_sandboxes,
            self.prune_workspaces,
            self.expire_gateway_keys,
            self.replenish_sandbox_pool,
        ]

    @activity.defn
    async def replenish_sandbox_pool(self) -> None:
        if isinstance(self.sandbox, K8sSandboxProvider):
            settings = self.settings
            self.sandbox.pool_size = settings.k8s_pool_size
            await self.sandbox.replenish(
                SandboxSpec(
                    image=settings.sandbox_image,
                    cpus=settings.sandbox_cpus,
                    memory_mb=settings.sandbox_memory_mb,
                    network=settings.sandbox_network,
                    runtime=settings.sandbox_runtime,
                    labels={"sca.state_transport": "sync"},
                ),
                settings.k8s_pool_size if settings.state_transport == "sync" else 0,
            )

    @activity.defn
    async def archive_events(self) -> None:
        await archive_finished(
            self.store, self.objects, timedelta(days=self.settings.archive_after_days)
        )

    @activity.defn
    async def prune_repo_cache(self) -> None:
        from sca.workspace.layers import LayerStore

        if self.settings.sandbox_driver == "docker" and self.settings.workspace_overlay:
            from sca.workspace.layers import command
            from sca.workspace.overlay import VOLUME, OverlayWorkspace

            names = await command("docker", "volume", "ls", "--filter",
                                  "label=sca.workspace=overlay", "--format", "{{.Name}}")
            for name in names.splitlines():
                if not VOLUME.fullmatch(name):
                    continue
                task_id = name.removeprefix("sca-ws-")
                try:
                    task = await self.store.get_task(str(UUID(task_id)))
                except (ValueError, NotFoundError):
                    task = None
                if task is not None and task.state not in TERMINAL_STATES:
                    continue
                await command("docker", "volume", "rm", name)
                directory = self.settings.data_dir.resolve() / "tasks" / task_id
                if task is None:
                    pin_name = hashlib.sha256(str(directory / "overlay").encode()).hexdigest()
                    for pin in (self.settings.data_dir / "layers").glob(f"*.refs/{pin_name}"):
                        pin.unlink(missing_ok=True)
                if task is None and (directory / "workspace.overlay").exists():
                    ws = OverlayWorkspace(directory, "sca/prune")
                    await ws.clear_upper()
                    (directory / "overlay.layer").unlink(missing_ok=True)
        total = await LayerStore(self.settings.data_dir / "layers").prune(
            self.settings.layer_retention_hours
        )
        activity.logger.info("Workspace layer bytes: %d", total)
        from sca.workspace.snapshots import SnapshotStore

        snapshot_bytes = await SnapshotStore(self.settings.data_dir / "snapshots").prune(
            self.settings.snapshot_retention_hours)
        activity.logger.info("Workspace snapshot bytes: %d", snapshot_bytes)
        await RepoCache(self.settings.data_dir / "cache" / "repos").prune(
            self.settings.repo_cache_max_entries
        )

    @activity.defn
    async def reap_sandboxes(self) -> None:
        for sandbox_id in await self.sandbox.list({"sca.task_id": ""}):
            if sandbox_id.startswith("sca-pool-") and isinstance(self.sandbox, K8sSandboxProvider):
                suffix = await self.sandbox.task_id(sandbox_id) or ""
            elif sandbox_id.startswith("sca-task-"):
                suffix = sandbox_id.removeprefix("sca-task-")
            else:
                continue
            if len(suffix) > 36 and suffix[36] != "-":
                continue
            try:
                task_id = str(UUID(suffix[:36]))
            except ValueError:
                continue
            # Check the label value too: names alone are not ownership evidence.
            if sandbox_id not in await self.sandbox.list({"sca.task_id": task_id}):
                continue
            try:
                task = await self.store.get_task(task_id)
            except NotFoundError:
                pass
            else:
                if task.state not in TERMINAL_STATES:
                    continue
            await self.sandbox.destroy(sandbox_id)

    @activity.defn
    async def prune_workspaces(self) -> None:
        root = self.settings.tasks_dir()
        if not _ordinary_directory(root):
            return
        now = datetime.now(UTC)
        expire_at = now - timedelta(days=self.settings.task_retention_days)
        cutoff = now - timedelta(hours=self.settings.workspace_retention_hours)
        for directory in root.iterdir():
            if not _ordinary_directory(directory):
                continue
            try:
                task_id = UUID(directory.name)
                if str(task_id) != directory.name:
                    continue
                task = await self.store.get_task(str(task_id))
            except (ValueError, NotFoundError):
                continue
            if task.state not in TERMINAL_STATES:
                continue
            if task.updated_at <= expire_at:
                try:
                    await self._expire_task_directory(directory, task_id)
                except (OSError, WorkspaceError):
                    _logger.warning("Task directory cleanup failed for %s", task_id)
                continue
            await cleanup_finished_cache(self.settings, self.store, task_id)
            if task.updated_at > cutoff:
                continue
            from sca.workspace.overlay import OverlayWorkspace

            overlay = (directory / "workspace.overlay").exists()
            workspace = directory / "overlay" if overlay else directory / "workspace"
            if not workspace.exists() and not workspace.is_symlink():
                continue
            manifest = await TaskStateStore(self.objects, task_id).manifest()
            if manifest is None or (task.result.get("commit") and
                                    manifest.get("commit") != task.result["commit"]):
                continue
            # Keep local state when the durable snapshot is missing or corrupt.
            for digest in manifest.get("sha256", {}).values():
                blob = await self.objects.get(f"tasks/{task_id}/state/blobs/{digest}")
                if hashlib.sha256(blob).hexdigest() != digest:
                    raise ValueError("Task state checksum mismatch")
            if not {"mirror.bundle", "agent_home.tar"}.issubset(manifest.get("sha256", {})):
                continue
            if overlay:
                ws = OverlayWorkspace(directory, "sca/prune", image=self.settings.sandbox_image)
                await ws.ensure_volume()
                await ws.cleanup()
                continue
            if workspace.is_symlink():
                workspace.unlink()
            else:
                await asyncio.to_thread(shutil.rmtree, workspace)
            (directory / "workspace.ready").unlink(missing_ok=True)

    async def _expire_task_directory(self, directory: Path, task_id: UUID) -> None:
        if self.settings.sandbox_driver == "docker" and (directory / "workspace.overlay").exists():
            from sca.workspace.overlay import OverlayWorkspace

            await OverlayWorkspace(directory, "sca/prune", image=self.settings.sandbox_image,
                                   docker_bin=self.settings.docker_bin).remove_volume()
        freed = await _remove_tree(self.settings, directory, Path("."))
        if freed is not None:
            await _cleanup_event(self.store, task_id, "task_directory", freed)

    @activity.defn
    async def expire_gateway_keys(self) -> None:
        if self.gateway is None:
            return
        for alias in await self.gateway.list_aliases("task-"):
            try:
                task_id = str(UUID(alias[5:41]))
            except ValueError:
                continue
            try:
                task = await self.store.get_task(task_id)
            except NotFoundError:
                continue
            if task.state in TERMINAL_STATES:
                await self.gateway.revoke_key(alias)


_logger = logging.getLogger(__name__)
_CACHE = Path("agent_home/.cache/opencode/bin")

# Use the same confined privilege as the overlay cleanup helper for sandbox-owned directories.
_REMOVE_TREE = """
import pathlib
import shutil
import subprocess
import sys

root = pathlib.Path('/cleanup')
target = root
for part in pathlib.Path(sys.argv[1]).parts:
    target = target / part
    if target.is_symlink() or not target.is_dir():
        sys.exit(0)
size = subprocess.check_output(['du', '-s', '-B1', '--', str(target)]).split()[0]
if target == root:
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
else:
    shutil.rmtree(target)
print(size.decode())
"""


def _ordinary_directory(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir()


def _cache_locations(directory: Path) -> Iterator[Path]:
    yield _CACHE
    for name in ("sub", "best_of"):
        parent = directory / name
        if _ordinary_directory(parent):
            for child in parent.iterdir():
                if _ordinary_directory(child):
                    yield Path(name, child.name) / _CACHE


async def _remove_tree(settings: Settings, directory: Path, relative: Path) -> int | None:
    from sca.workspace.layers import command, disk_bytes

    if not _ordinary_directory(directory):
        return None
    target, measured = directory, None
    try:
        for part in relative.parts:
            target /= part
            if not _ordinary_directory(target):
                return None
        measured = await disk_bytes(target)
        await asyncio.to_thread(shutil.rmtree, target)
        return measured
    except FileNotFoundError:
        return None
    except (PermissionError, WorkspaceError):
        if settings.sandbox_driver != "docker":
            raise
    output = await command(
        settings.docker_bin, "run", "--rm", "--network=none", "--read-only",
        "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
        "--security-opt=no-new-privileges", "--memory=128m", "--cpus=1",
        "--user=0", "--volume", f"{directory}:/cleanup", "--entrypoint=python3",
        settings.sandbox_image, "-c", _REMOVE_TREE, relative.as_posix(),
    )
    if not output:
        return None
    if not relative.parts:
        directory.rmdir()
    return measured if measured is not None else int(output)


async def _cleanup_event(store: Store, task_id: UUID, target: str, freed: int) -> None:
    try:
        await store.append_event(task_id, "maintenance.disk_reclaimed",
                                 {"target": target, "freed_bytes": freed},
                                 dedup_key=f"maintenance.{target}")
    except ConflictError:
        # Event archives are immutable. The maintenance log records cleanup of archived tasks.
        _logger.info("maintenance.disk_reclaimed task_id=%s target=%s freed_bytes=%d",
                     task_id, target, freed)


async def cleanup_finished_cache(settings: Settings, store: Store, task_id: UUID) -> None:
    if not settings.cache_cleanup or not _ordinary_directory(settings.tasks_dir()):
        return
    directory = settings.task_dir(str(task_id))
    if not _ordinary_directory(directory):
        return
    freed: list[int] = []
    try:
        for relative in _cache_locations(directory):
            size = await _remove_tree(settings, directory, relative)
            if size is not None:
                freed.append(size)
    except (OSError, WorkspaceError):
        _logger.warning("OpenCode cache cleanup failed for %s", task_id)
    if freed:
        await _cleanup_event(store, task_id, "opencode_cache", sum(freed))
