"""Read the execution graph using the same tenant scope as task history."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from sca.api.authz import authorize
from sca.api.conversation import ConversationPage, task_page
from sca.api.guest_history import history_dag, history_payload
from sca.api.live_status import LiveSnapshot
from sca.api.live_task import LIVE_TASK_EVENTS, TaskTrace
from sca.core.errors import NotFoundError
from sca.core.models import Event, Principal, Task
from sca.core.task_dag import TaskDAG, build_task_dag, linked_tasks
from sca.db.repo import Store
from sca.live_heartbeat import read_beats

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


@router.get('/tasks/{task_id}/conversation', response_model_exclude_none=True)
async def read_task_conversation(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize('read', guest=True))],
    cursor: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConversationPage:
    return await task_page(request.app.state.store.for_tenant(user.tenant_id), task_id, user, cursor, limit)


@router.get('/tasks/{task_id}/live', response_model_exclude_none=True)
async def read_task_live(
    task_id: UUID, request: Request, response: Response,
    user: Annotated[Principal, Depends(authorize('read', guest=True))],
) -> LiveSnapshot:
    settings = request.app.state.settings
    store = request.app.state.store.for_tenant(user.tenant_id)
    task, events, last_event = await store.live_task_history(task_id, LIVE_TASK_EVENTS)
    snapshot = TaskTrace(task, events, settings.agent_backend).snapshot(
        datetime.now(UTC), settings.stall_warn_s, last_event, read_beats(settings.data_dir, 'task', task_id),
    )
    response.headers['Cache-Control'] = 'no-store'
    return LiveSnapshot.model_validate(history_payload(user, snapshot.model_dump(exclude_none=True)))
