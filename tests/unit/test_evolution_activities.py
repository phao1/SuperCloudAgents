"""Model prompt isolation and activity input/output contracts with fakes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sca.config import Settings
from sca.evolve.activities import EvolutionActivities
from sca.evolve.models import EvolutionConfig


def setup(tmp_path):
    row = {'case_id': 'dev-case', 'task_id': uuid4(), 'acceptance_passed': False,
           'verification_passed': True, 'trajectory_passed': False,
           'trajectory_findings': [{'rule': 'verify'}], 'turns': 2, 'cost_usd': .01}
    store = SimpleNamespace(
        get_eval_run=AsyncMock(return_value={'variant': {'split': 'dev'}, 'results': [row]}),
        evolution_evidence=AsyncMock(return_value=[{'command': 'pytest', 'output_tail': 'failed'}]),
        get_evolution=AsyncMock(return_value={'journal': [
            {'hypothesis': 'prior idea', 'decision': 'rejected', 'dev_metrics': {'pass_rate': 0},
             'holdout_metrics': {'private': 'HOLDOUT_MARKER'}, 'reason': 'HOLDOUT_MARKER'}]}),
        create_eval_run=AsyncMock())
    root = SimpleNamespace(for_tenant=lambda tenant: store)
    gateway = SimpleNamespace(complete=AsyncMock(side_effect=[
        json.dumps({'diagnoses': ['Check verification failures']}),
        json.dumps([{'hypothesis': 'verify changes', 'overlay': {'tool_guidance': 'Run tests'}}])]))
    activities = EvolutionActivities(Settings(_env_file=None), root, gateway)
    activities.suite = AsyncMock(return_value={
        'hash': 'h', 'name': 's', 'cases': [{'spec': {'instruction': 'a b c d e f g'},
            'acceptance_command': 'pytest', 'path': str(tmp_path)}]})
    request = {'id': str(uuid4()), 'tenant_id': 'eval', 'created_by': 'test',
               'suite_hash': 'h', 'config': EvolutionConfig(suite='s', agent_version='v').model_dump(),
               'overlay': {}, 'dev_run_id': str(uuid4())}
    return activities, store, gateway, request


async def test_dev_reflection_journal_and_validation(tmp_path):
    activities, store, gateway, request = setup(tmp_path)
    result = await activities.propose(request)
    assert result[0]['static_check']['valid']
    assert result[0]['hypothesis'] == 'verify changes'
    messages = str(gateway.complete.call_args_list)
    assert 'HOLDOUT_MARKER' not in messages
    assert 'prior idea' in messages
    assert 'untrusted' in messages
    assert 'failed' in messages
    assert gateway.complete.call_args.args[1] == 'deepseek-flash'
    store.evolution_evidence.assert_awaited_once()


async def test_holdout_reflection_forbidden(tmp_path):
    activities, store, gateway, request = setup(tmp_path)
    store.get_eval_run.return_value['variant']['split'] = 'holdout'
    with pytest.raises(ValueError, match='dev evidence'):
        await activities.propose(request)
    gateway.complete.assert_not_awaited()


@pytest.mark.parametrize('response', ['not json', '{}', '[{"hypothesis": 3}]'])
async def test_bad_model_output_is_journaled_invalid(tmp_path, response):
    activities, _, gateway, request = setup(tmp_path)
    gateway.complete.side_effect = ['{"diagnoses": ["pattern"]}', response]
    result = await activities.propose(request)
    assert result and not result[0]['static_check']['valid']


async def test_prepare_persists_split_and_overlay_before_child(tmp_path):
    activities, store, _, request = setup(tmp_path)
    payload = await activities.prepare_run({**request, 'run_id': str(uuid4()),
                                           'split': 'dev', 'remaining': .7})
    saved = store.create_eval_run.call_args.args[0]
    assert saved['variant'] == payload
    assert payload['split'] == 'dev'
    assert payload['suite_hash'] == 'h'
    assert payload['budget'] == .7
    assert payload['variant']['agent_overlay'] == {}
    assert saved['status'] == 'queued'


async def test_fenced_or_wrapped_proposals_parse_with_room_for_full_prompts(tmp_path):
    activities, _, gateway, request = setup(tmp_path)
    body = json.dumps({'candidates': [{'hypothesis': 'h', 'overlay': {'tool_guidance': 'Run tests'}}]})
    gateway.complete.side_effect = ['```json\n{"diagnoses": ["p"]}\n```', 'Here:\n```json\n' + body + '\n```']
    result = await activities.propose(request)
    assert result[0]['hypothesis'] == 'h' and result[0]['static_check']['valid']
    assert result[0]['diagnosis'] == {'diagnoses': ['p']}
    assert gateway.complete.call_args.kwargs['max_tokens'] >= 4096
