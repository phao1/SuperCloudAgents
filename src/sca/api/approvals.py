"""List pending decisions from the current tenant's existing workflows."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from sca.api.authz import authorize
from sca.core.models import Principal
from sca.evolve.progress import one_line

router = APIRouter(prefix='/v1')
ApprovalKind = Literal['task_plan', 'task_result', 'task_fix', 'task_selection', 'evolution_candidate', 'harvest']


class ApprovalItem(BaseModel):
    id: UUID
    kind: ApprovalKind
    title: str
    created_at: datetime
    href: str


class ApprovalInbox(BaseModel):
    count: int
    items: list[ApprovalItem]


def _item(row: dict) -> ApprovalItem:
    id_ = row['id']
    kind: ApprovalKind
    if row['source'] == 'evolution':
        kind, href = 'evolution_candidate', f'#/evolutions/runs/{row["evolution_id"]}/approval/{id_}'
    elif row['source'] == 'harvest':
        kind, href = 'harvest', f'#/evolutions/harvest/approval/{id_}'
    else:
        href = f'#/tasks/{id_}/approval'
        if row['selection'] == 'human' and row['chosen'] is None:
            kind = 'task_selection'
        elif row['plan_review']:
            kind = 'task_plan'
        elif row['fix_kind'] == 'opencode-fix':
            kind = 'task_fix'
        else:
            kind = 'task_result'
    return ApprovalItem(id=id_, kind=kind, title=one_line(row['title'] or str(id_), 160),
                        created_at=row['created_at'], href=href)


@router.get('/approvals')
async def list_approvals(request: Request, user: Annotated[Principal, Depends(authorize('read'))]) -> ApprovalInbox:
    store = request.app.state.store.for_tenant(user.tenant_id)
    rows = await store.pending_approvals(
        include_evolutions=user.tenant_id == 'eval' and user.role in ('member', 'admin'),
        include_harvest=user.role == 'admin',
    )
    items = sorted(map(_item, rows), key=lambda item: (item.created_at, item.id), reverse=True)
    return ApprovalInbox(count=len(items), items=items)
