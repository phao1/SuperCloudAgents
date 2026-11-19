"""Activity-local child scope; shared activities keep their normal retry and usage behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sca.core.interfaces import ObjectStore
from sca.core.models import Event, EventType, SandboxSpec, Task
from sca.db.repo import Store
from sca.kinds.coding.activities import CodingActivities, branch_name
from sca.workspace.git import GitWorkspace


class ChildStore:
    def __init__(self, store: Store, name: str) -> None:
        self.store, self.name = store, name
        self.prefix = f"subtask.{name}."

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def get_task(self, task_id: str) -> Task:
        task = await self.store.get_task(task_id)
        result = {
            k.removeprefix(self.prefix): v
            for k, v in task.result.items()
            if k.startswith(self.prefix)
        }
        if task.result.get("prompt_version"):
            result["prompt_version"] = task.result["prompt_version"]
        return task.model_copy(update={"result": result})

    async def update_result(self, task_id: str, patch: dict) -> None:
        await self.store.update_result(task_id, {self.prefix + k: v for k, v in patch.items()})

    async def append_event(
        self,
        task_id: str,
        type_: EventType,
        payload: dict,
        dedup_key: str | None = None,
    ) -> Event | None:
        return await self.store.append_event(
            task_id,
            type_,
            {**payload, "subtask": self.name},
            dedup_key=f"{self.name}.{dedup_key}" if dedup_key else None,
        )

    async def get_session_id(self, task_id: str) -> str | None:
        return (await self.get_task(task_id)).result.get("session_id")

    async def set_session(self, task_id: str, backend: str, backend_session_id: str) -> None:
        await self.update_result(task_id, {"session_id": backend_session_id})

    async def add_artifact(self, task_id: str, name: str, *args: Any) -> Any:
        return await self.store.add_artifact(task_id, f"subtask-{self.name}-{name}", *args)


class ChildObjects:
    def __init__(self, objects: ObjectStore, name: str) -> None:
        self.objects, self.name = objects, name

    def _key(self, key: str) -> str:
        return key.replace("/state/", f"/subtasks/{self.name}/state/")

    async def get(self, key: str) -> bytes:
        return await self.objects.get(self._key(key))

    async def put(self, key: str, *args: Any) -> str:
        if "/state/" in key:
            return await self.objects.put(self._key(key), *args)
        path = Path(key)
        return await self.objects.put(
            str(path.with_name(f"subtask-{self.name}-{path.name}")), *args
        )


class ChildActivities(CodingActivities):
    def __init__(self, parent: CodingActivities, name: str) -> None:
        super().__init__(
            settings=parent.settings,
            store=ChildStore(parent.store, name),
            sandbox=parent.sandbox,
            agent=parent.agent,
            objects=ChildObjects(parent.objects, name),
            gateway=parent.gateway,
            secrets=parent.secrets,
            backends=parent.backends,
            worker_identity=parent.worker_identity,
        )
        self.name = name

    def _task_dir(self, task_id: str) -> Path:
        return self.settings.task_dir(task_id) / "sub" / self.name

    def _workspace(self, task_id: str) -> GitWorkspace:
        return GitWorkspace(self._task_dir(task_id), f"{branch_name(task_id)}-{self.name}")

    async def _create_sandbox(self, task_id: str) -> tuple[str, str]:
        # The provider wrapper changes only identity and labels, retaining all mounts/policy.
        provider = self.sandbox
        name = self.name

        class ScopedProvider:
            def __getattr__(self, attr: str) -> Any:
                return getattr(provider, attr)

            async def create(self, sandbox_name: str, spec: SandboxSpec) -> str:
                spec.labels["sca.subtask"] = name
                return await provider.create(f"{sandbox_name}-{name}", spec)

        self.sandbox = ScopedProvider()
        try:
            return await super()._create_sandbox(task_id)
        finally:
            self.sandbox = provider

    async def destroy_sandbox(self, task_id: str) -> None:
        task = await self.store.get_task(task_id)
        sandbox_id = task.result.get("sandbox_id", f"sca-task-{task_id}-{self.name}")
        await self.sandbox.destroy(sandbox_id)
        await self.store.append_event(
            task_id,
            EventType.SANDBOX_DESTROYED,
            {"sandbox_id": sandbox_id},
            dedup_key="sandbox.destroyed",
        )
