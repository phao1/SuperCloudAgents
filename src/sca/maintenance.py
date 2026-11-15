"""Independent, bounded periodic housekeeping activities."""

from datetime import timedelta
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


@workflow.defn
class MaintenanceWorkflow:
    @workflow.run
    async def run(self) -> None:
        for name in (
            "archive_events",
            "prune_repo_cache",
            "reap_sandboxes",
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
