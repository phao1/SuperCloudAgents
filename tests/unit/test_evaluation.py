from pathlib import Path

import pytest

from sca.eval.compare import compare, markdown, sign_test
from sca.eval.suites import load_suite
from sca.kernel.prompts import render_prompt
from sca.kinds.analysis import prompts as analysis
from sca.kinds.analysis.spec import AnalysisSpec
from sca.kinds.coding import prompts as coding
from sca.kinds.coding.spec import CodingSpec


@pytest.mark.parametrize('commands', [[], ['pytest'], ['pytest', 'ruff check .']])
def test_v1_coding_identical(commands):
    spec = {'instruction': '  Do this.\n', 'verify_commands': commands}
    assert render_prompt(spec, 'initial') == coding.initial_prompt(spec['instruction'], commands)
    assert render_prompt(spec, 'fix', exit_code=1, output=' error \n') == coding.fix_prompt(
        ' && '.join(commands), 1, ' error \n')
    assert render_prompt(spec, 'feedback', feedback=' change \n') == coding.feedback_prompt(' change \n')


def test_v1_analysis_identical():
    spec = {'question': 'How many?', 'checks': ['test x'], 'report_format': 'html'}
    assert render_prompt(spec, 'initial') == analysis.initial_prompt(spec)
    assert render_prompt(spec, 'fix', exit_code=1, output='failed') == analysis.fix_prompt(1, 'failed')
    assert render_prompt(spec, 'feedback', feedback='change') == analysis.feedback_prompt('change')


@pytest.mark.parametrize('model,fields', [(CodingSpec, {'repo_url': 'x', 'instruction': 'x'}),
                                        (AnalysisSpec, {'question': 'x', 'inputs': ['12345678-1234-1234-1234-123456789abc']})])
def test_common_spec(model, fields):
    from uuid import uuid4
    file_id = str(uuid4())
    spec = model(**fields, acceptance_files=file_id, acceptance_command='test -f hidden',
                 auto_finish=True, agent_backend='fake', prompt_version='v1')
    assert spec.model_dump()['acceptance_files'] == file_id
    with pytest.raises(ValueError):
        model(**fields, acceptance_files=file_id)
    with pytest.raises(ValueError):
        model(**fields, prompt_version='../secret')


def test_suite_has_twelve_cases():
    suite = load_suite(Path('evals/coding-basic'))
    assert len(suite['cases']) == 12
    assert suite['hash'] == load_suite(Path('evals/coding-basic'))['hash']
    assert all(case['spec']['verify_command'] != case['acceptance_command'] for case in suite['cases'])


def test_compare_pairs_cases_without_pseudoreplication():
    def run(passed):
        return {'suite_hash': 'same', 'results': [
            {'case_id': case, 'repeat': repeat, 'acceptance_passed': ok, 'turns': repeat + 1,
             'cost_usd': .01, 'duration_s': 2, 'tokens': 10}
            for case, ok in passed.items() for repeat in range(3)]}
    report = compare(run({'a': True, 'b': False}), run({'a': False, 'b': True}))
    assert report['B_wins'] == report['A_wins'] == 1
    assert report['conclusion'] == 'not enough data'
    assert report['A']['turns'] == {'mean': 2, 'min': 1, 'max': 3}
    assert '| a | 1 | 0 |' in markdown(report)
    assert sign_test(6, 0) == .03125
    assert sign_test(3, 3) == 1
    with pytest.raises(ValueError):
        compare(run({}), {'suite_hash': 'different', 'results': []})


@pytest.mark.parametrize('visible,hidden,expected', [(True, False, 'SUCCEEDED'),
                                                  (False, True, 'FAILED')])
async def test_hidden_failure_never_enters_fix_loop(monkeypatch, visible, hidden, expected):
    from sca.kernel.activities import CheckpointResult, TurnResult, VerifyResult
    from sca.kernel.workflow import TaskWorkflow

    calls = []
    spec = {'max_fix_attempts': 0, 'turn_timeout_s': 30, 'auto_finish': True,
            'acceptance_files': 'hidden-id', 'acceptance_command': 'secret-check',
            'hooks': {name: name for name in ('prepare', 'initial_prompt', 'verify',
                                             'checkpoint', 'publish', 'fix_prompt')},
            'extensions_before': False}

    async def execute(name, arg=None, **kwargs):
        calls.append((name, arg))
        if name == 'load_task':
            return spec
        if name == 'initial_prompt':
            return 'visible instructions'
        if name == 'run_agent_turn':
            assert 'secret' not in arg.prompt
            return TurnResult(None, 'done')
        if name == 'checkpoint':
            return CheckpointResult('abc', True)
        if name == 'verify':
            return VerifyResult(visible, 0 if visible else 1, '')
        if name == 'run_acceptance':
            return hidden
    monkeypatch.setattr('sca.kernel.workflow.workflow.execute_activity', execute)
    monkeypatch.setattr('sca.kernel.workflow.workflow.patched', lambda _id: True)
    assert await TaskWorkflow()._run('task') == expected
    names = [name for name, _ in calls]
    assert names.count('run_agent_turn') == names.count('run_acceptance') == 1
    assert names.index('run_acceptance') < next(i for i, (n, a) in enumerate(calls)
                                               if n == 'set_state' and a.state == 'AWAITING_REVIEW')
    assert 'fix_prompt' not in names


async def test_acceptance_uses_clean_separate_checkout(tmp_path):
    import io
    import subprocess
    import tarfile
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from temporalio.testing import ActivityEnvironment

    from sca.config import Settings
    from sca.core.models import ExecExit
    from sca.kernel.activities import KernelActivities
    from sca.workspace.git import GitWorkspace

    seed = tmp_path / 'seed'
    seed.mkdir()
    (seed / 'app.py').write_text('committed')
    def git(*args):
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@invalid', *args],
                       cwd=seed, check=True, capture_output=True)
    git('init', '-b', 'main')
    git('add', '.')
    git('commit', '-m', 'fixture')
    task_id, file_id = str(uuid4()), str(uuid4())
    settings = Settings(data_dir=tmp_path / 'state', repo_cache=False)
    ws = GitWorkspace(settings.task_dir(task_id), f'sca/{task_id[:8]}')
    await ws.prepare(str(seed), 'main')
    await ws.fetch()
    (ws.workspace / 'app.py').write_text('uncommitted changes must not be copied')
    (ws.workspace / 'untracked').write_text('must not be copied')
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        entry = tarfile.TarInfo('hidden-marker')
        entry.size = 6
        tar.addfile(entry, io.BytesIO(b'hidden'))
    task = SimpleNamespace(tenant_id='tenant', result={})
    store = SimpleNamespace(get_task=AsyncMock(return_value=task),
                            get_file=AsyncMock(), complete_acceptance=AsyncMock())
    store.for_tenant = lambda tenant: store
    sandbox = SimpleNamespace(destroy=AsyncMock())
    async def create(name, spec):
        assert name.startswith('acceptance-')
        root = Path(spec.mounts[0].source)
        assert root != ws.workspace
        assert (root / 'app.py').read_text() == 'committed'
        assert (root / 'hidden-marker').read_text() == 'hidden'
        assert not (root / 'untracked').exists()
        assert len(spec.mounts) == 1
        return 'acceptance-sandbox'
    sandbox.create = create
    objects = SimpleNamespace(get=AsyncMock(return_value=archive.getvalue()))
    kernel = KernelActivities(settings=settings, store=store, sandbox=sandbox,
                              objects=objects, agent=SimpleNamespace(name='fake'))
    kernel._spec = AsyncMock(return_value=SimpleNamespace(
        acceptance_files=file_id, acceptance_command='secret-check', image=None, turn_timeout_s=30))
    kernel._ensure_state = AsyncMock()
    kernel._run = AsyncMock(return_value=(ExecExit(exit_code=1), 'hidden failed'))
    assert await ActivityEnvironment().run(kernel.run_acceptance, task_id) is False
    assert not (ws.workspace / 'hidden-marker').exists()
    objects.get.assert_awaited_once_with(f'tenants/tenant/files/{file_id}')
    sandbox.destroy.assert_awaited_once_with('acceptance-sandbox')
    store.complete_acceptance.assert_awaited_once_with(task_id, False, 1, 'hidden failed')
    task.result['acceptance_passed'] = False
    assert await ActivityEnvironment().run(kernel.run_acceptance, task_id) is False
    assert kernel._run.await_count == 1


@pytest.mark.parametrize('name,kind,size', [('../escape', None, 1), ('/absolute', None, 1),
                                         ('.git/config', None, 1), ('large', None, 100),
                                         ('link', 'symlink', 0)])
def test_acceptance_archive_rejects_unsafe_entries(tmp_path, name, kind, size):
    import io
    import tarfile

    from sca.kernel.acceptance import extract_acceptance

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        entry = tarfile.TarInfo(name)
        entry.size = size
        if kind == 'symlink':
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/etc/passwd'
        archive.addfile(entry, io.BytesIO(b'x' * size))
    with pytest.raises(ValueError):
        extract_acceptance(output.getvalue(), tmp_path, 10)


def test_backend_selection_does_not_mutate_worker_default(tmp_path):
    from types import SimpleNamespace

    from sca.config import Settings
    from sca.kernel.activities import KernelActivities

    default, alternate = SimpleNamespace(name='default'), SimpleNamespace(name='alternate')
    kernel = KernelActivities(settings=Settings(data_dir=tmp_path), store=None, sandbox=None,
                              objects=None, agent=default,
                              backends={'default': default, 'alternate': alternate})
    assert kernel._backend(SimpleNamespace(agent_backend='alternate')) is alternate
    assert kernel._backend(SimpleNamespace(agent_backend=None)) is default
    assert kernel.agent is default
    from sca.core.errors import InvalidTaskError
    with pytest.raises(InvalidTaskError):
        kernel._backend(SimpleNamespace(agent_backend="unknown"))


def test_cli_compare_and_regression_gate(monkeypatch):
    from typer.testing import CliRunner

    from sca.eval import cli

    def run(passed):
        return {'suite_hash': 'same', 'status': 'completed', 'results': [
            {'case_id': 'a', 'repeat': 0, 'acceptance_passed': passed,
             'turns': 1, 'cost_usd': .01, 'duration_s': 2, 'tokens': 10}]}
    async def read(run_id):
        return run(run_id == 'before')
    async def start(*args, **kwargs):
        return 'after'
    monkeypatch.setattr(cli, 'read_run', read)
    monkeypatch.setattr(cli, 'start', start)
    runner = CliRunner()
    report = runner.invoke(cli.app, ['compare', 'before', 'after', '--markdown'])
    assert report.exit_code == 0, report.output
    assert '| a | 1 | 0 |' in report.output
    assert 'not enough data' in report.output
    gated = runner.invoke(cli.app, ['gate', 'evals/coding-basic', '--baseline', 'before'])
    assert gated.exit_code == 1
