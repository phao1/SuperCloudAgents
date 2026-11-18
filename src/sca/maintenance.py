"""Independent, bounded periodic housekeeping activities."""

import asyncio
import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from sca.archive import archive_finished
    from sca.config import Settings
    from sca.core.errors import NotFoundError
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
        root = self.settings.data_dir.resolve() / "tasks"
        if not root.is_dir():
            return
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.workspace_retention_hours)
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                task_id = str(UUID(directory.name))
                task = await self.store.get_task(task_id)
            except (ValueError, NotFoundError):
                continue
            if task.state not in TERMINAL_STATES or task.updated_at > cutoff:
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
