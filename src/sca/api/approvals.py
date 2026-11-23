"""List pending decisions from the current tenant's existing workflows."""

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from sca.api.authz import authorize
from sca.core.models import Principal
from sca.evolve.progress import one_line
from sca.history.privacy import clean_text

router = APIRouter(prefix='/v1')
ApprovalKind = Literal['task_plan', 'task_result', 'task_fix', 'task_selection', 'evolution_candidate', 'harvest']


class ApprovalItem(BaseModel):
    id: UUID
    kind: ApprovalKind
    title: str
    created_at: datetime
    href: str
    task_kind: str | None = None
    backend: str | None = None
    project_name: str | None = None


class ApprovalInbox(BaseModel):
    count: int
    items: list[ApprovalItem]


def _title(value: str, fallback: str) -> str:
    fence = ''
    for line in clean_text(value).splitlines():
        line = re.sub(r'^(?: {0,3}>[ \t]?)+', '', line).strip()
        marker = re.match(r'(`{3,}|~{3,})(.*)', line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ''
            continue
        if marker and (marker[1][0] == '~' or '`' not in marker[2]):
            fence = marker[1]
            continue
        if not line or re.fullmatch(r'(?:[-*_]\s*){3,}', line):
            continue
        if re.match(r'#{1,6}\s', line):
            line = re.sub(r'^#{1,6}\s+|\s+#+$', '', line)
        line = re.sub(r'^(?:[-+*]|\d{1,9}[.)])\s+', '', line)
        line = re.sub(r'!?\[([^\[\]]+)\]\([^)]*\)', r'\1', line[:640])
        line = re.sub(r'(`+|\*{1,3}|\b_{1,3}|~~)(\S(?:.*?\S)?)\1', r'\2', line)
        if title := one_line(line, 160):
            return title
    return fallback


def _item(row: dict, default_backend: str) -> ApprovalItem:
    id_ = row['id']
    backend, task_kind = row.get('backend'), row.get('task_kind')
    kind: ApprovalKind
    if row['source'] == 'evolution':
        kind, href = 'evolution_candidate', f'#/evolutions/runs/{row["evolution_id"]}/approval/{id_}'
    elif row['source'] == 'harvest':
        kind, href = 'harvest', f'#/evolutions/harvest/approval/{id_}'
    else:
        href = f'#/tasks/{id_}/approval'
        backend = backend or default_backend
        if row['selection'] == 'human' and row['chosen'] is None:
            kind = 'task_selection'
        elif row['plan_review']:
            kind = 'task_plan'
        elif row['fix_kind'] == 'opencode-fix':
            kind = 'task_fix'
        else:
            kind = 'task_result'
    title = row['title'] or ''
    return ApprovalItem(
        id=id_, kind=kind, created_at=row['created_at'], href=href,
        title=(one_line(title, 160) or str(id_)) if kind == 'harvest' else _title(title, str(id_)),
        task_kind=task_kind, backend=one_line(backend, 60) if backend else None,
        project_name=one_line(row['project_name'], 160) if row.get('project_name') else None,
    )


@router.get('/approvals')
async def list_approvals(
    request: Request, response: Response, user: Annotated[Principal, Depends(authorize('read'))],
) -> ApprovalInbox:
    store = request.app.state.store.for_tenant(user.tenant_id)
    rows = await store.pending_approvals(
        include_evolutions=user.tenant_id == 'eval' and user.role in ('member', 'admin'),
        include_harvest=user.role == 'admin',
    )
    default_backend = request.app.state.settings.agent_backend
    items = sorted((_item(row, default_backend) for row in rows),
                   key=lambda item: (item.created_at, item.id), reverse=True)
    response.headers['Cache-Control'] = 'no-store'
    return ApprovalInbox(count=len(items), items=items)
