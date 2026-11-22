"""Split scheduling and metadata use a durable snapshot, independent of suite edits."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from sca.api import evals
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import Principal
from sca.eval.activities import EvalActivities
from sca.eval.compare import compare
from sca.eval.suites import case_metadata, load_suite, select_cases
from sca.kinds.coding.spec import CodingSpec


@pytest.fixture(autouse=True)
def inline_fixture_io(monkeypatch):
    # These unit tests exercise scheduling and payloads, not thread-pool behavior.
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr('asyncio.to_thread', inline)


@pytest.fixture
def split_suite(tmp_path):
    root = tmp_path / 'suite'
    root.mkdir()
    (root / 'suite.yaml').write_text('name: split-test\nkind: coding\ndefaults:\n  memory_enabled: false\n')
    for name, split in [('a','dev'), ('b','holdout')]:
        case = root / 'cases' / name
        (case / 'repo').mkdir(parents=True)
        (case / 'repo/app.py').write_text('value = 1\n')
        (case / 'acceptance').mkdir()
        (case / 'acceptance/hidden.py').write_text('assert True\n')
        (case / 'acceptance.yaml').write_text('command: python hidden.py\n')
        (case / 'task.yaml').write_text(f'split: {split}\ncategory: bug-fix\ninstruction: Fix it\n')
    return root


def test_suite_split_metadata_and_full_hash(split_suite):
    suite = load_suite(split_suite)
    assert case_metadata(suite['cases']) == {
        'a': {'split':'dev','category':'bug-fix'},
        'b': {'split':'holdout','category':'bug-fix'},
    }
    assert [case['id'] for case in select_cases(suite,'dev')] == ['a']
    assert [case['id'] for case in select_cases(suite,'holdout')] == ['b']
    assert len(select_cases(suite)) == 2
    assert 'split' not in suite['cases'][0]['spec']
    assert 'category' not in suite['cases'][0]['spec']
    original = suite['hash']
    (split_suite / 'cases/b/repo/app.py').write_text('value = 2\n')
    assert load_suite(split_suite)['hash'] != original


def test_suite_hash_excludes_only_root_readme(split_suite):
    original = load_suite(split_suite)['hash']
    readme = split_suite / 'README.md'
    readme.write_text('Suite documentation')
    expected_legacy_hash = hashlib.sha256(b''.join(
        path.relative_to(split_suite).as_posix().encode() + b'\0' + path.read_bytes()
        for path in sorted(split_suite.rglob('*')) if path.is_file()
    )).hexdigest()
    suite = load_suite(split_suite)
    assert suite['hash'] == original
    assert suite['legacy_hash'] == expected_legacy_hash != original
    readme.write_text('Edited documentation')
    assert load_suite(split_suite)['hash'] == original
    readme.unlink()
    assert load_suite(split_suite)['hash'] == original
    (split_suite / 'cases/a/repo/README.md').write_text('Fixture documentation')
    assert load_suite(split_suite)['hash'] != original


@pytest.mark.parametrize('relative', ['cases/a/task.yaml', 'cases/a/acceptance/hidden.py'])
def test_suite_hash_includes_case_and_hidden_acceptance(split_suite, relative):
    original = load_suite(split_suite)['hash']
    path = split_suite / relative
    path.write_text(path.read_text() + '\n# fixture edit\n')
    assert load_suite(split_suite)['hash'] != original


def test_suite_readme_symlink_is_rejected(split_suite):
    (split_suite / 'README.md').symlink_to(split_suite / 'suite.yaml')
    with pytest.raises(ValueError, match='symlinks'):
        load_suite(split_suite)


@pytest.mark.parametrize('metadata', ['split: test', 'split: null', 'category: []', 'category: ""'])
def test_suite_invalid_metadata(split_suite, metadata):
    (split_suite / 'cases/a/task.yaml').write_text(metadata+'\ninstruction: Fix\n')
    with pytest.raises(ValueError):
        load_suite(split_suite)


def test_legacy_suites_and_empty_split():
    suite = load_suite(Path('evals/coding-basic'))
    assert len(select_cases(suite)) == 12
    assert all('split' not in case for case in suite['cases'])
    with pytest.raises(ValueError, match='no cases'):
        select_cases(suite, 'dev')
    with pytest.raises(ValueError, match='split must'):
        select_cases(suite, 'typo')


def test_benchmark_shape_and_specs():
    suite = load_suite(Path('evals/opencode-evolve'))
    assert Counter(case['split'] for case in suite['cases']) == {'dev':20,'holdout':10}
    categories = Counter(case['category'] for case in suite['cases'])
    assert len(categories) == 10 and set(categories.values()) == {3}
    for category in categories:
        assert Counter(case['split'] for case in suite['cases'] if case['category'] == category) == {'dev':2,'holdout':1}
    for case in suite['cases']:
        spec = CodingSpec(repo_url='/fixture', **suite['defaults'], **case['spec'])
        assert spec.image == 'python:3.12'
        assert spec.max_fix_attempts == 1 and spec.turn_timeout_s == 240
        for path in Path(case['path']).rglob('*.py'):
            compile(path.read_bytes(), str(path), 'exec')


@pytest.mark.parametrize('split,count', [(None,2),('dev',1),('holdout',1)])
async def test_prepare_filters_and_snapshots(split_suite, tmp_path, split, count):
    store = SimpleNamespace(create_eval_run=AsyncMock(), start_eval_run=AsyncMock())
    acts = EvalActivities(Settings(_env_file=None, data_dir=tmp_path), store)
    request = {'id':str(uuid4()),'suite':str(split_suite),'split':split,'variant':{}}
    plan = await ActivityEnvironment().run(acts.prepare,request)
    assert len(plan['cases']) == count
    if split:
        assert all(case['split'] == split for case in plan['cases'])
    saved = store.create_eval_run.call_args.args[0]
    assert saved['variant']['split'] == split
    assert saved['variant']['case_metadata'] == case_metadata(plan['cases'])
    assert saved['suite_hash'] == load_suite(split_suite)['hash']
    assert all(case['suite_hash'] == saved['suite_hash'] for case in plan['cases'])


async def test_prepare_rejects_suite_changes(split_suite, tmp_path):
    store = SimpleNamespace(finish_eval_run=AsyncMock(),create_eval_run=AsyncMock())
    acts = EvalActivities(Settings(_env_file=None,data_dir=tmp_path),store)
    with pytest.raises(ApplicationError, match='Suite changed') as exc:
        await ActivityEnvironment().run(acts.prepare,{'id':str(uuid4()),'suite':str(split_suite),'suite_hash':'old','split':'dev'})
    assert exc.value.type == 'SuiteChanged'
    assert exc.value.non_retryable
    store.create_eval_run.assert_not_called()
    store.finish_eval_run.assert_awaited_once()  # recorded even for pre-patch workflow histories


async def test_prepare_accepts_legacy_hash_without_rewriting_lineage(split_suite, tmp_path):
    (split_suite / 'README.md').write_text('Legacy suite documentation')
    suite = load_suite(split_suite)
    store = SimpleNamespace(create_eval_run=AsyncMock(), start_eval_run=AsyncMock())
    acts = EvalActivities(Settings(_env_file=None, data_dir=tmp_path), store)
    plan = await acts.prepare({'id': str(uuid4()), 'suite': str(split_suite),
                               'suite_hash': suite['legacy_hash']})
    assert store.create_eval_run.call_args.args[0]['suite_hash'] == suite['legacy_hash']
    assert all(case['suite_hash'] == suite['hash'] for case in plan['cases'])


@pytest.mark.parametrize('change,error_type', [
    ('fixture', 'SuiteChanged'), ('unknown_case', 'InvalidEvalInput'),
    ('malformed_yaml', 'InvalidEvalInput'),
])
async def test_submit_validation_fails_before_api_calls(split_suite, tmp_path, change, error_type):
    store = SimpleNamespace(create_eval_run=AsyncMock(), start_eval_run=AsyncMock())
    acts = EvalActivities(Settings(_env_file=None, data_dir=tmp_path), store)
    request = {'id': str(uuid4()), 'suite': str(split_suite)}
    case = (await acts.prepare(request))['cases'][0]
    if change == 'fixture':
        (split_suite / 'cases/a/acceptance/hidden.py').write_text('assert False\n')
    elif change == 'unknown_case':
        case['id'] = 'unknown'
    else:
        (split_suite / 'suite.yaml').write_text('name: [\n')
    with pytest.raises(ApplicationError) as exc:
        await acts.submit({**request, 'case': case})
    assert exc.value.type == error_type
    assert exc.value.non_retryable


@pytest.mark.parametrize('overlay', [{}, {'system_prompt':'中文 {{tools}}', 'tool_descriptions':{'read':'exact\ntext'}, 'nested':{'opaque':[1,False,None]}}])
async def test_submit_passes_overlay_unchanged(split_suite, tmp_path, monkeypatch, overlay):
    acts = EvalActivities(Settings(_env_file=None,data_dir=tmp_path/'data'), None)
    monkeypatch.setattr(acts,'git',AsyncMock())
    posted = []
    task_id = str(uuid4())
    def handle(request):
        if request.url.path == '/v1/files':
            return httpx.Response(200,json={'tenant_id':'eval','id':str(uuid4())})
        posted.append(json.loads(request.content))
        return httpx.Response(200,json={'tenant_id':'eval','id':task_id})
    monkeypatch.setattr(acts,'http',lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://test'))
    suite = load_suite(split_suite)
    case = {**suite['cases'][0], 'kind':'coding','suite_hash':suite['hash']}
    (split_suite / 'README.md').write_text('New suite documentation')
    request = {'id':str(uuid4()), 'suite':str(split_suite), 'case':case, 'variant':{'agent_overlay':overlay}, 'case_budget':.1,'iteration':0}
    assert await ActivityEnvironment().run(acts.submit,request) == task_id
    assert posted[0]['spec']['agent_overlay'] == overlay
    assert 'split' not in posted[0]['spec'] and 'category' not in posted[0]['spec']


async def test_dsh_eval_variant_reaches_task_spec(split_suite, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / 'data')
    acts = EvalActivities(settings, None)
    monkeypatch.setattr(acts, 'git', AsyncMock())
    task_id = str(uuid4())
    overlay = {'system_prompt': 'Check the verification output.'}

    def handle(request):
        if request.url.path == '/v1/files':
            return httpx.Response(201, json={'tenant_id': 'eval', 'id': str(uuid4())})
        spec = CodingSpec.model_validate(json.loads(request.content)['spec'])
        assert spec.agent_backend == 'dsh' and spec.agent_version == settings.dsh_version
        assert spec.agent_overlay.system_prompt == overlay['system_prompt']
        return httpx.Response(201, json={'tenant_id': 'eval', 'id': task_id})

    monkeypatch.setattr(acts, 'http', lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url='http://test',
    ))
    suite = load_suite(split_suite)
    case = {**suite['cases'][0], 'kind': 'coding', 'suite_hash': suite['hash']}
    assert await acts.submit({
        'id': str(uuid4()), 'suite': str(split_suite), 'case': case, 'case_budget': .05, 'iteration': 0,
        'variant': {'agent_backend': 'dsh', 'agent_version': settings.dsh_version, 'agent_overlay': overlay},
    }) == task_id


@pytest.mark.parametrize('status_code,expected_error', [
    (422, ApplicationError), (429, httpx.HTTPStatusError), (503, httpx.HTTPStatusError),
])
async def test_collect_http_failure_classification(tmp_path, monkeypatch, status_code, expected_error):
    acts = EvalActivities(Settings(_env_file=None, data_dir=tmp_path), None)
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code))
    monkeypatch.setattr(acts, 'http', lambda: httpx.AsyncClient(transport=transport, base_url='http://test'))
    with pytest.raises(expected_error) as exc:
        await acts.collect({'task_id': str(uuid4())})
    if status_code == 422:
        assert exc.value.type == 'InvalidEvalInput'
        assert exc.value.non_retryable


@pytest.mark.parametrize('split', ['dev','holdout',None])
def test_request_accepts_split_and_overlay(split):
    overlay = {'system_prompt':'unchanged', 'nested':{'values':[None,0,'中']}}
    request = evals.RunRequest(suite='suite',split=split,variant={'agent_overlay':overlay},budget=1,case_budget=.1)
    assert request.variant.model_dump(exclude_none=True)['agent_overlay'] == overlay
    assert request.split == split


@pytest.mark.parametrize('fields', [{'split':'test'},{'split':1},{'variant':{'agent_overlay':[]}},{'variant':{'agent_overlay':'text'}},{'variant':{'unknown':{}}}])
def test_request_rejects_invalid_fields(fields):
    with pytest.raises(ValidationError):
        evals.RunRequest(suite='suite',budget=1,case_budget=.1,**fields)


async def test_retest_preserves_verified_legacy_baseline_hash(split_suite, monkeypatch):
    (split_suite / 'README.md').write_text('Legacy documentation')
    suite = load_suite(split_suite)
    monkeypatch.setattr(evals, 'suite_path', lambda _: split_suite)
    monkeypatch.setattr(evals, 'get_settings', lambda: Settings(_env_file=None))
    store = SimpleNamespace(create_eval_run=AsyncMock())
    store.for_tenant = lambda _: store
    client = SimpleNamespace(start_workflow=AsyncMock())
    state = SimpleNamespace(store=store, orchestrator=SimpleNamespace(client=client))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    body = evals.RunRequest(suite='suite', budget=1, case_budget=.1)
    user = Principal(tenant_id='eval', subject='test')

    await evals.enqueue_run(body, request, user, expected_suite_hash=suite['legacy_hash'])

    assert store.create_eval_run.call_args.args[0]['suite_hash'] == suite['legacy_hash']
    assert client.start_workflow.call_args.args[1]['suite_hash'] == suite['hash']


async def test_api_stores_snapshot_and_returns_metadata(split_suite, monkeypatch):
    monkeypatch.setattr(evals,'suite_path',lambda name:split_suite)
    rows = {}
    async def create(run):
        rows[str(run['id'])] = {**run,'results':[]}
    async def get(run_id):
        return rows[run_id]
    store = SimpleNamespace(create_eval_run=AsyncMock(side_effect=create),get_eval_run=AsyncMock(side_effect=get))
    store.for_tenant = lambda _:store
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    app = FastAPI()
    app.state.store = store
    app.state.authenticator = StaticTokenAuthenticator({'eval-test':'eval'})
    app.state.orchestrator = SimpleNamespace(client=temporal)
    app.include_router(evals.router)
    overlay = {'system_prompt':'保持中文\n{{tools}}','tool_descriptions':{'read':'Read carefully'}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Authorization':'Bearer eval-test'}) as client:
        body = {'suite':'suite','split':'dev','variant':{'agent_overlay':overlay},'budget':1,'case_budget':.1}
        response = await client.post('/v1/evals/runs',json=body)
        assert response.status_code == 201, response.text
        run_id = response.json()['id']
        saved = rows[run_id]
        assert saved['variant']['split'] == 'dev'
        assert saved['variant']['variant']['agent_overlay'] == overlay
        assert saved['variant']['case_metadata'] == {'a':{'split':'dev','category':'bug-fix'}}
        payload = temporal.start_workflow.call_args.args[1]
        assert payload['split'] == 'dev' and payload['variant']['agent_overlay'] == overlay
        saved['results'] = [{'case_id':'a','repeat':0,'acceptance_passed':True,'cost_usd':0}]
        # Metadata comes from the recorded snapshot, even after fixtures change.
        (split_suite/'cases/a/task.yaml').write_text('instruction: changed\nsplit: holdout\n')
        detail = (await client.get('/v1/evals/runs/'+run_id)).json()
        assert detail['split'] == 'dev'
        assert detail['results'][0]['split'] == 'dev'
        assert detail['results'][0]['category'] == 'bug-fix'
        second = {**detail,'results':[dict(detail['results'][0])]}
        assert compare(detail,second)['cases'][0]['A'] == 1
        for patch in [{'split':'invalid'},{'variant':{'agent_overlay':[]}}]:
            assert (await client.post('/v1/evals/runs',json={**body,**patch})).status_code == 422
        # An old suite has no matching split, so it must not silently run all cases.
        monkeypatch.setattr(evals,'suite_path',lambda name:Path('evals/coding-basic'))
        assert (await client.post('/v1/evals/runs',json=body)).status_code == 422
