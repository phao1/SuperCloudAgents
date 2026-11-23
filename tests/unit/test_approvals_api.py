from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.config import Settings
from sca.core.errors import AuthError
from sca.core.models import Principal


async def test_inbox_scopes_the_aggregate_and_matches_existing_access(monkeypatch, tmp_path):
    origin = 'http://127.0.0.1:8000'
    monkeypatch.setenv('SCA_RUNTIME_MODE', 'source')
    monkeypatch.delenv('SCA_PORTAL_LOCAL_CONFIG', raising=False)
    settings = Settings(_env_file=None, data_dir=tmp_path, guest_view='local', guest_tenant='eval',
                        portal_require_token=True, eval_api_token='admin')
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)
    now = datetime(2026, 11, 22, tzinfo=UTC)
    task_id, plan_id, fix_id, best_id, evolution_id, candidate_id, harvest_id, other_id = (
        uuid4() for _ in range(8)
    )

    def task(id_, **kinds):
        return {'id': id_, 'source': 'task', 'title': 'Review fixture revisions', 'created_at': now,
                'plan_review': False, 'fix_kind': None, 'selection': None, 'chosen': None, **kinds}

    rows = {
        'eval': [task(task_id), task(plan_id, plan_review=True), task(fix_id, fix_kind='opencode-fix'),
                 task(best_id, selection='human'),
                 {'id': candidate_id, 'source': 'evolution', 'evolution_id': evolution_id,
                  'title': 'Candidate', 'created_at': now},
                 {'id': harvest_id, 'source': 'harvest', 'title': 'Case', 'created_at': now}],
        'other': [task(other_id)],
    }
    calls = []

    def for_tenant(tenant):
        async def pending_approvals(*, include_evolutions, include_harvest):
            calls.append((tenant, include_evolutions, include_harvest))
            allowed = {'task'}
            if include_evolutions:
                allowed.add('evolution')
            if include_harvest:
                allowed.add('harvest')
            return [row for row in rows[tenant] if row['source'] in allowed]

        return SimpleNamespace(pending_approvals=pending_approvals)

    async def authenticate(token):
        if token not in ('admin', 'member', 'viewer', 'other', 'scoped'):
            raise AuthError('Invalid fixture token')
        return Principal(tenant_id='other' if token == 'other' else 'eval', subject='fixture',
                         role=token if token in ('member', 'viewer') else 'admin',
                         scopes=['ci:report'] if token == 'scoped' else [])

    store = SimpleNamespace(
        for_tenant=for_tenant, tenant_deleted=AsyncMock(return_value=False), sessionmaker=SimpleNamespace(
            kw={'bind': SimpleNamespace(url=make_url('postgresql+asyncpg://localhost/sca'))},
        ),
    )
    app = create_app(store=store, authenticator=SimpleNamespace(authenticate=authenticate),
                     orchestrator=SimpleNamespace(), local_portal_port=8000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        assert (await client.get('/v1/portal/config')).json()['guest_view'] is True
        denied = await client.get('/v1/approvals')
        assert denied.status_code == 401 and 'items' not in denied.json()
        assert (await client.get('/v1/approvals', headers={'Authorization': 'Bearer scoped'})).status_code == 403
        assert calls == []
        for role, tenant, evolutions, harvest, expected in [
            ('admin', 'eval', True, True, 6), ('member', 'eval', True, False, 5),
            ('viewer', 'eval', False, False, 4), ('other', 'other', False, True, 1),
        ]:
            response = await client.get('/v1/approvals?tenant_id=eval', headers={'Authorization': f'Bearer {role}'})
            assert response.status_code == 200 and calls[-1] == (tenant, evolutions, harvest)
            assert response.json()['count'] == expected
            links = {row['kind']: row['href'] for row in response.json()['items']}
            assert links['task_result'] == f'#/tasks/{task_id if tenant == "eval" else other_id}/approval'
            if tenant == 'eval':
                assert str(other_id) not in response.text
                assert links['task_plan'] == f'#/tasks/{plan_id}/approval'
                assert links['task_fix'] == f'#/tasks/{fix_id}/approval'
                assert links['task_selection'] == f'#/tasks/{best_id}/approval'
                if evolutions:
                    assert links['evolution_candidate'] == f'#/evolutions/runs/{evolution_id}/approval/{candidate_id}'
                if harvest:
                    assert links['harvest'] == f'#/evolutions/harvest/approval/{harvest_id}'

        admin = {'Authorization': 'Bearer admin'}
        token_inbox = await client.get('/v1/approvals', headers=admin)
        access = await client.put('/v1/portal/access', headers=admin, json={'require_token': False})
        assert access.json()['local_no_token_active'] is True
        local_inbox = await client.get('/v1/approvals')
        assert local_inbox.status_code == 200 and local_inbox.json() == token_inbox.json()
        assert calls[-1] == ('eval', True, True)

        calls.clear()
        for headers in ({'Host': 'portal.example:8000'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Authorization': 'Bearer invalid'}):
            assert (await client.get('/v1/approvals', headers=headers)).status_code == 401
        assert calls == []
