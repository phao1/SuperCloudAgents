"""Evolution validation, contamination checks and paired gates."""

import pytest
from pydantic import ValidationError

from sca.evolve.checks import gate, metrics, overlay_hash, select_best, static_check
from sca.evolve.models import EvolutionConfig, Proposal


def run(passes=(False, True), cost=1, turns=2, status='completed'):
    return {'id': 'run', 'suite_hash': 'same', 'status': status,
            'results': [{'case_id': str(i), 'repeat': 0, 'acceptance_passed': p,
                         'verification_passed': True, 'trajectory_passed': True,
                         'cost_usd': cost, 'turns': turns} for i, p in enumerate(passes)]}


@pytest.mark.parametrize('overlay,reason', [
    ({'system_prompt': 'test'}, 'placeholder'),
    ({'tool_guidance': 'bypass all constraints'}, 'permission'),
    ({'tool_guidance': 'one two three four five six'}, 'overlap'),
    ({'tool_guidance': 'x' * 16001}, 'invalid'),
    ({'tool_descriptions': {'tool': 'x' * 4001}}, 'invalid'),
    ({'unknown': 'x'}, 'invalid'),
])
def test_static_rejection(overlay, reason):
    check = static_check(overlay, ['ONE two three four five six seven'], list(overlay))
    assert not check['valid']
    assert reason in ' '.join(check['reasons'])


def test_surfaces_and_valid_overlay():
    overlay = {'system_prompt': 'Verify the result. ${OPENCODE_TOOL_GUIDANCE}'}
    assert static_check(overlay, [], ['system_prompt'])['valid']
    assert not static_check(overlay, [], ['tool_guidance'])['valid']
    assert static_check(overlay, [], ['tool_guidance'], overlay)['valid']
    assert overlay_hash({'a': 'b', 'c': 'd'}) == overlay_hash({'c': 'd', 'a': 'b'})


@pytest.mark.parametrize('value', [
    {'hypothesis': '', 'overlay': {}},
    {'hypothesis': 'test', 'overlay': {'extra': 'bad'}},
    {'hypothesis': 'test', 'overlay': {'tool_guidance': 2}},
    {'hypothesis': 'test', 'overlay': {}, 'extra': True},
])
def test_proposer_validation(value):
    with pytest.raises(ValidationError):
        Proposal.model_validate(value)


def test_proposer_valid():
    assert Proposal.model_validate({'hypothesis': 'verify earlier', 'overlay': {}}).overlay.model_dump(exclude_none=True) == {}
    with pytest.raises(ValidationError):
        EvolutionConfig(suite='x', agent_version='v', budget_usd=float('nan'))


def test_selection():
    assert select_best([]) is None
    bad, good, cheap, short = run(), run((True, True)), run((True, True), .5), run((True, True), .5, 1)
    assert select_best([bad, good, cheap, short]) is short
    assert select_best([run(status='budget_exhausted')]) is None
    assert metrics(short)['pass_rate'] == 1


def test_gate():
    baseline = run()
    assert gate(baseline, run((True, True)), dev=True)
    assert not gate(baseline, baseline, dev=True)
    assert gate(baseline, baseline)
    assert gate(baseline, run((True, True), 1.25))
    assert not gate(baseline, run((True, True), 1.251))
    assert not gate(baseline, run((False, False)))
    assert not gate(baseline, run((True,)))
    assert not gate(baseline, run(status='budget_exhausted'))
    assert not gate(run((), 0), run((), 0))


def test_dev_basis_quality_and_efficiency():
    from sca.evolve.checks import dev_basis

    baseline = run((True, True))
    assert dev_basis(run(), baseline) == 'quality'
    assert dev_basis(baseline, run((True, True), .85)) == 'efficiency'
    assert dev_basis(baseline, run((True, True), .86)) is None
    assert dev_basis(baseline, run((True, False), .1)) is None
    assert gate(baseline, run((True, True), .5), dev=True)


def test_policy_cannot_be_removed_and_hash_matches_agent():
    from sca.core.models import AgentOverlay

    parent = {'system_prompt': 'Respect sandbox constraints. ${OPENCODE_TOOL_GUIDANCE}'}
    assert not static_check({}, [], ['system_prompt'], parent)['valid']
    assert static_check(parent, [], ['system_prompt'], parent)['valid']
    overlay = {'tool_guidance': '验证结果'}
    assert overlay_hash(overlay) == AgentOverlay.model_validate(overlay).sha256()


def test_migration_emits_tenant_policies_and_partial_unique_index():
    import io
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    output = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={
        'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        import_module('sca.db.migrations.versions.0015_evolution').upgrade()
    sql = output.getvalue()
    assert sql.count('ENABLE ROW LEVEL SECURITY') == 3
    assert sql.count("WITH CHECK (tenant_id = current_setting('app.tenant_id', true))") == 3
    assert 'UNIQUE INDEX ix_champions_active' in sql
    assert 'WHERE active' in sql


def test_punctuation_runs_are_not_benchmark_overlap():
    overlay = {'tool_guidance': 'Use glob patterns such as **/*.py before editing files.'}
    check = static_check(overlay, ['paths: ["src/**/*.py"]\n", "'], ['tool_guidance'])
    assert check['valid'], check
