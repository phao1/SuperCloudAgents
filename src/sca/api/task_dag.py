"""Read the execution graph using the same tenant scope as task history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from sca.api.authz import authorize
from sca.api.guest_history import history_dag
from sca.core.errors import NotFoundError
from sca.core.models import Event, Principal, Task
from sca.core.task_dag import TaskDAG, build_task_dag, linked_tasks
from sca.db.repo import Store

router = APIRouter(prefix="/v1")


async def _history(store: Store, task_id: UUID) -> list[Event]:
    result: list[Event] = []
    after, page_size = 0, 5000
    while True:
        page = await store.list_events(task_id, after, page_size)
        result.extend(page)
        if len(page) < page_size:
            return result
        after = page[-1].seq


@router.get("/tasks/{task_id}/dag")
async def task_dag(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read", guest=True))],
) -> TaskDAG:
    store = request.app.state.store.for_tenant(user.tenant_id)
    root = await store.get_task(task_id, user.tenant_id)
    tasks: list[Task] = [root]
    seen = {root.id}
    events = []
    for task in tasks:
        history = await _history(store, task.id)
        events.extend(history)
        for child in await store.list_task_children(task.id):
            if child.id not in seen:
                tasks.append(child)
                seen.add(child.id)
        for child_id in linked_tasks(task, history):
            if child_id not in seen:
                seen.add(child_id)
                try:
                    tasks.append(await store.get_task(child_id, user.tenant_id))
                except NotFoundError:
                    # Earlier links can outlive task retention.
                    continue
    return history_dag(user, build_task_dag(root, events, tasks[1:]))
