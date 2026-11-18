import json

import pytest
from test_pi_fixture import REAL_FAILURE_TRANSCRIPT, TRANSCRIPT

from sca.agents.pi import KEY_ENV, PiBackend, parse_pi_line, render_config
from sca.config import Settings
from sca.core.errors import AgentTurnError
from sca.core.models import AgentContext, ExecExit, ExecOutput


def test_parser():
    tools = {}
    events = [e for line in TRANSCRIPT.splitlines() for e in parse_pi_line(line, tools)]
    assert [e.type for e in events] == ['command', 'file_change', 'message', 'usage']
    assert events[0].data['command'] == 'pwd'
    assert events[0].data['duration_ms'] == 3
    assert events[1].data['changes'] == [{'path': 'app.py', 'kind': 'update'}]
    assert events[-1].data == {'input_tokens': 14, 'output_tokens': 2,
                                    'cached_input_tokens': 3, 'cost': 0.001}
    assert tools == {}


@pytest.mark.parametrize('line', ['bad', 'null', '[]', '{}', '{"type":[]}',
                                  '{"type":"message_end","message":[]}',
                                  '{"type":"tool_execution_end","toolCallId":[]}'])
def test_malformed(line):
    parse_pi_line(line, {})


def test_config_and_mount(tmp_path):
    config = json.loads(render_config('http://gateway/v1/', 'deepseek-flash'))
    provider = config['providers']['sca']
    assert provider['apiKey'] == '$SCA_MODEL_KEY'
    assert provider['baseUrl'] == 'http://gateway/v1'
    assert provider['models'] == [{'id': 'deepseek-flash'}]
    ctx = AgentContext(task_id='t', agent_home_host=str(tmp_path))
    backend = PiBackend('deepseek-flash')
    assert backend.sandbox_additions(ctx).mounts[0].target == '/agent_home'
    backend.state_transport = 'sync'
    assert backend.sandbox_additions(ctx).mounts == []
    settings = Settings(_env_file=None)
    assert settings.pi_version == '1.5.2'
    assert settings.pi_sandbox_image == 'sca-sandbox-pi:latest'


@pytest.mark.parametrize('code,timed_out,complete', [(0, False, True), (1, False, True),
                                                   (0, True, True), (0, False, False)])
async def test_turn(tmp_path, code, timed_out, complete):
    calls = []
    class Sandbox:
        async def exec(self, sid, argv, **kwargs):
            calls.append(argv)
            assert kwargs['stdin'] == b'prompt'
            assert kwargs['timeout_s'] == 30
            assert kwargs['env'][KEY_ENV] == 'ephemeral-test-key'
            assert 'ephemeral-test-key' not in json.dumps(argv)
            assert 'ephemeral-test-key' not in kwargs['env']['SCA_PI_CONFIG']
            assert '--no-approve' in argv and '--no-extensions' in argv
            for line in TRANSCRIPT.splitlines():
                if not complete and 'agent_settled' in line:
                    continue
                yield ExecOutput(stream='stdout', text=line.replace('Fixed', 'ephemeral-test-key'))
            yield ExecExit(exit_code=code, timed_out=timed_out)

    backend = PiBackend('deepseek-flash')
    ctx = AgentContext(task_id='t', agent_home_host=str(tmp_path),
                       model_base_url='http://gateway/v1', model_api_key='ephemeral-test-key')
    async def collect(context=ctx, session=None):
        return [e async for e in backend.run_turn(Sandbox(), 's', context, 'prompt',
                                                session_id=session, timeout_s=30)]
    if code or timed_out or not complete:
        with pytest.raises(AgentTurnError):
            await collect()
    else:
        events = await collect()
        assert events[-1].data['final_message'] == '[REDACTED]'
        sid = events[0].data['session_id']
        resumed = await collect(session=sid)
        assert not any(e.type == 'session_started' for e in resumed)
        assert calls[-1][-1] == sid
    with pytest.raises(AgentTurnError):
        await collect(ctx.model_copy(update={'model_api_key': None}))
    with pytest.raises(AgentTurnError):
        await collect(session='../escape')
    with pytest.raises(AgentTurnError):
        await collect(ctx.model_copy(update={'agent_version': 'other'}))


def test_model_error():
    events = parse_pi_line(json.dumps({'type': 'message_end', 'message': {
        'role': 'assistant', 'stopReason': 'error', 'errorMessage': 'sensitive'}}))
    assert events[-1].type == 'error'
    assert 'sensitive' not in str(events)


def test_recorded_failure():
    events = [e for line in REAL_FAILURE_TRANSCRIPT.splitlines() for e in parse_pi_line(line)]
    assert [e.type for e in events] == ['usage', 'error'] * 4
    assert events[0].data['input_tokens'] == 0


def test_launcher_writes_only_env_reference(tmp_path):
    import os
    import subprocess
    import sys

    from sca.agents.pi import LAUNCH

    home = tmp_path / 'agent'
    binary = tmp_path / 'pi'
    binary.write_text('#!/bin/sh\ncat\n')
    binary.chmod(0o700)
    config = render_config('http://gateway/v1', 'model')
    result = subprocess.run(
        [sys.executable, '-c', LAUNCH, '--print'], input=b'prompt', capture_output=True,
        timeout=5, env={'PATH': str(tmp_path) + ':' + os.defpath,
                        'PI_CODING_AGENT_DIR': str(home), 'SCA_PI_CONFIG': config,
                        KEY_ENV: 'launcher-test-credential'}, check=True,
    )
    assert result.stdout == b'prompt'
    assert (home / 'models.json').read_text() == config
    assert 'launcher-test-credential' not in (home / 'models.json').read_text()
