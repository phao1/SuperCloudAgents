"""Hidden acceptance and ordinary task auto-finish, using the real sandbox."""

import io
import subprocess
import tarfile

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def require_docker():
    try:
        probe = subprocess.run(['docker', 'info'], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip('Docker unreachable')
    if probe.returncode:
        pytest.skip('Docker unreachable')


def hidden_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        info = tarfile.TarInfo('c4-hidden-marker.py')
        data = b'raise AssertionError("deliberately stricter than visible checks")\n'
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


async def test_hidden_acceptance_isolated_no_fix_turn(stack, source_repo):
    uploaded = await stack.http.post('/v1/files', files={'file': ('hidden.tar', hidden_archive())})
    uploaded.raise_for_status()
    command = 'python c4-hidden-marker.py'
    script = 'find / -name c4-hidden-marker.py 2>/dev/null; echo C4_FIND_FINISHED'
    task_id = await stack.submit({
        'repo_url': str(source_repo), 'instruction': f'Check the workspace.\n```sca-fake\n{script}\n```',
        'verify_command': 'true', 'max_fix_attempts': 2, 'auto_finish': True,
        'acceptance_files': uploaded.json()['id'], 'acceptance_command': command,
    })
    task = await stack.wait_for(task_id, 'SUCCEEDED')
    assert task['result']['acceptance_passed'] is False
    assert task['result']['verification_passed'] is True
    assert task['result']['turns'] == 1
    events = await stack.events(task_id)
    turns = [event['payload'] for event in events if event['type'] == 'agent.turn_started']
    assert len(turns) == 1
    assert all(command not in turn['prompt'] for turn in turns)
    assert turns[0]['prompt_version'] == 'v1'
    outputs = [event['payload'].get('text', '') + event['payload'].get('output_tail', '') for event in events
               if event['type'] in {'agent.message', 'agent.command'}]
    assert any('C4_FIND_FINISHED' in text for text in outputs)
    assert not any('/workspace/c4-hidden-marker.py' in text for text in outputs)
    acceptance = [event['payload'] for event in events if event['type'] == 'acceptance.completed']
    assert len(acceptance) == 1 and acceptance[0]['exit_code'] != 0


async def test_task_prompt_version_is_recorded(stack, source_repo, tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    import sca.kernel.prompts as renderer

    templates = Path(renderer.__file__).parents[1] / 'kinds' / 'coding' / 'prompts'
    root = tmp_path / 'templates'
    target = root / 'kinds' / 'coding' / 'prompts'
    shutil.copytree(templates / 'v1', target / 'v1')
    shutil.copytree(templates / 'v1', target / 'v2')
    initial = target / 'v2' / 'initial.txt'
    initial.write_text('C4 SECOND PROMPT VERSION\n' + initial.read_text())
    monkeypatch.setattr(renderer, '__file__', str(root / 'kernel' / 'prompts.py'))
    prompts = []
    for version in ('v1', 'v2'):
        task_id = await stack.submit({
            'repo_url': str(source_repo), 'instruction': 'Inspect.\n```sca-fake\ntrue\n```',
            'verify_command': 'true', 'prompt_version': version, 'auto_finish': True,
        })
        await stack.wait_for(task_id, 'SUCCEEDED')
        event = next(e for e in await stack.events(task_id) if e['type'] == 'agent.turn_started')
        assert event['payload']['prompt_version'] == version
        prompts.append(event['payload']['prompt'])
    assert 'C4 SECOND PROMPT VERSION' not in prompts[0]
    assert 'C4 SECOND PROMPT VERSION' in prompts[1]
