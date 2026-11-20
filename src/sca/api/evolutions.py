"""Admin API for eval-tenant evolution and champion rollback."""

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import NoResultFound
from temporalio.client import Client
from temporalio.service import RPCError

from sca.api.authz import authorize
from sca.api.evals import require_eval
from sca.config import get_settings
from sca.core.models import Principal
from sca.db.repo import Store
from sca.eval.suites import load_suite
from sca.evolve.models import EvolutionConfig
from sca.evolve.workflow import EvolutionWorkflow

router = APIRouter(prefix='/v1')
Admin = Annotated[Principal, Depends(authorize('tokens'))]


def scoped(request: Request, user: Principal) -> Store:
    require_eval(user)
    return request.app.state.store.for_tenant(user.tenant_id)


async def client_for(request: Request) -> Client:
    client = getattr(request.app.state.orchestrator, 'client', None)
    if client is None:
        settings = get_settings()
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    return client


async def detail(request: Request, user: Principal, evolution_id: UUID) -> dict:
    try:
        return await scoped(request, user).get_evolution(str(evolution_id))
    except NoResultFound:
        raise HTTPException(404, '演进记录不存在') from None


@router.post('/evolutions', status_code=201)
async def start(body: EvolutionConfig, request: Request, user: Admin) -> dict:
    store = scoped(request, user)
    settings = get_settings()
    root = settings.eval_suites_dir.resolve()
    path = Path(body.suite)
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    if path.is_symlink() or resolved.parent != root or not resolved.is_dir():
        raise HTTPException(422, '请选择已安装的评测套件')
    try:
        suite = await asyncio.to_thread(load_suite, resolved)
        splits = {c['spec'].get('split', c.get('split')) for c in suite['cases']}
        if not {'dev', 'holdout'} <= splits:
            raise ValueError('missing splits')
    except (ValueError, OSError):
        raise HTTPException(422, '评测套件需要开发集和留出集') from None
    config = body.model_dump()
    config['suite'] = str(resolved)
    evolution_id = uuid4()
    payload = {'id': str(evolution_id), 'tenant_id': user.tenant_id, 'created_by': user.subject,
               'config': config, 'suite_hash': suite['hash']}
    await store.create_evolution({'id': evolution_id, 'status': 'queued', 'config': config,
                                  'created_by': user.subject})
    try:
        async with asyncio.timeout(10):
            client = await client_for(request)
            await client.start_workflow(EvolutionWorkflow.run, payload, id=f'evolve-{evolution_id}',
                                        task_queue=settings.task_queue)
    except (RuntimeError, OSError, TimeoutError, RPCError):
        await store.update_evolution(str(evolution_id), {'status': 'failed'})
        raise HTTPException(503, '演进服务暂不可用') from None
    return {'id': str(evolution_id), 'status': 'queued'}


@router.get('/evolutions')
async def listing(request: Request, user: Admin) -> list[dict]:
    return await scoped(request, user).list_evolutions()


@router.get('/evolutions/{evolution_id}')
async def get(evolution_id: UUID, request: Request, user: Admin) -> dict:
    return await detail(request, user, evolution_id)


class Decision(BaseModel):
    candidate_id: UUID


async def signal(request: Request, user: Principal, evolution_id: UUID, name: str,
                 candidate_id: UUID | None = None) -> dict:
    row = await detail(request, user, evolution_id)
    if row['status'] not in ('queued', 'running', 'awaiting_approval'):
        raise HTTPException(409, '演进已结束')
    if name != 'stop' and (row['status'] != 'awaiting_approval' or not any(
        str(c['id']) == str(candidate_id) and c['decision'] == 'pending'
        and c['reason'] == 'awaiting approval' for c in row['candidates']
    )):
        raise HTTPException(409, '该候选未等待审批')
    try:
        async with asyncio.timeout(10):
            client = await client_for(request)
            handle = client.get_workflow_handle(f'evolve-{evolution_id}')
            if candidate_id is None:
                await handle.signal(name)
            else:
                await handle.signal(name, str(candidate_id))
    except (RuntimeError, OSError, TimeoutError, RPCError):
        raise HTTPException(503, '演进服务暂不可用') from None
    return {'status': '已发送'}


@router.post('/evolutions/{evolution_id}/stop')
async def stop(evolution_id: UUID, request: Request, user: Admin) -> dict:
    return await signal(request, user, evolution_id, 'stop')


@router.post('/evolutions/{evolution_id}/approve')
async def approve(evolution_id: UUID, body: Decision, request: Request, user: Admin) -> dict:
    return await signal(request, user, evolution_id, 'approve', body.candidate_id)


@router.post('/evolutions/{evolution_id}/reject')
async def reject(evolution_id: UUID, body: Decision, request: Request, user: Admin) -> dict:
    return await signal(request, user, evolution_id, 'reject', body.candidate_id)


@router.get('/champions')
async def champions(request: Request, user: Admin, agent_version: str | None = None,
                    model: str | None = None) -> list[dict]:
    return await scoped(request, user).list_champions(agent_version, model)


@router.post('/champions/{champion_id}/activate')
async def activate(champion_id: UUID, request: Request, user: Admin) -> dict:
    try:
        return await scoped(request, user).activate_champion(str(champion_id))
    except NoResultFound:
        raise HTTPException(404, '冠军记录不存在') from None
