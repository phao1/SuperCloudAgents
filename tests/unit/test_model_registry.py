import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from sca.agents.dsh import render_patch
from sca.gateway.configuration import local_default_model
from sca.gateway.model_providers import (
    COMPOSE_FILE,
    REGISTRY_FILE,
    ProviderInput,
    ProviderRegistry,
    RegistryError,
    docker_gateway_command,
    gateway_base_url,
    gateway_config,
    read_registry,
)

ROOT = Path(__file__).resolve().parents[2]


def local_provider(**changes):
    return ProviderInput.model_validate({
        'name': 'Local provider', 'type': 'openai-compatible', 'base_url': 'http://localhost:8001/v1',
        'models': [{'name': 'local-coder', 'upstream_model': 'owner/coder:v2', 'context_window': 32768}],
        **changes,
    })


@pytest.mark.parametrize('deepseek', [False, True])
def test_gateway_config_keeps_builtins_and_sets_custom_pricing_without_secrets(tmp_path, deepseek):
    fixture_key = "fixture-only-$literal'key"
    registry = ProviderRegistry().with_provider(uuid4(), local_provider(api_key=fixture_key))
    anthropic = local_provider(type='anthropic-compatible', base_url='https://models.example/api/v1', models=[
        {'name': 'claude-proxy', 'upstream_model': 'claude-4', 'input_usd_per_million': 3,
         'output_usd_per_million': 15},
    ])
    registry = registry.with_provider(uuid4(), anthropic)
    registry.save(tmp_path)
    loaded = read_registry(tmp_path)
    assert loaded == registry
    if sys.platform != 'win32':
        assert (tmp_path / REGISTRY_FILE).stat().st_mode & 0o777 == 0o600
    config_path = gateway_config(ROOT, tmp_path, loaded, deepseek=deepseek, docker=True)
    text = config_path.read_text(encoding='utf-8')
    assert fixture_key not in text and fixture_key not in repr(loaded)
    config = yaml.safe_load(text)
    assert config['general_settings']['proxy_batch_write_at'] == 1
    routes = {row['model_name']: row for row in config['model_list']}
    assert ('deepseek-flash' in routes) is deepseek
    if deepseek:
        template = yaml.safe_load((ROOT / 'deploy/litellm/config.yaml').read_text())
        for row in template['model_list']:
            assert routes[row['model_name']] == row
    params = routes['local-coder']['litellm_params']
    assert params['model'] == 'custom_openai/owner/coder:v2'
    assert params['api_base'] == 'http://host.docker.internal:8001/v1'
    assert params['api_key'] == f'os.environ/{loaded.providers[0].key_env}'
    assert params['input_cost_per_token'] == params['output_cost_per_token'] == 0
    assert routes['local-coder']['model_info']['max_input_tokens'] == 32768
    params = routes['claude-proxy']['litellm_params']
    assert params['model'] == 'anthropic/claude-4' and params['api_base'] == 'https://models.example/api'
    assert params['input_cost_per_token'] == 0.000003 and params['output_cost_per_token'] == 0.000015
    assert params['api_key'] == 'sca-no-upstream-key'
    assert fixture_key not in loaded.model_dump_json()
    assert fixture_key not in local_provider(api_key=fixture_key).model_dump_json()

    command = docker_gateway_command(ROOT, tmp_path, loaded, 'docker-wrapper', ['--force-recreate'])
    override = (tmp_path / COMPOSE_FILE).read_text(encoding='utf-8')
    assert fixture_key not in override and all(fixture_key not in arg for arg in command)
    service = json.loads(override)['services']['litellm']
    assert service['environment'] == [loaded.providers[0].key_env]
    assert ('extra_hosts' in service) is (sys.platform == 'linux')
    assert command[-2:] == ['--force-recreate', 'litellm']


@pytest.mark.parametrize(('url', 'rewritten'), [
    ('http://localhost:8001/v1/', 'http://host.docker.internal:8001/v1'),
    ('http://127.0.0.2:8001/api/v1', 'http://host.docker.internal:8001/api/v1'),
    ('https://[::1]/api', 'https://host.docker.internal/api'),
    ('http://[::ffff:127.0.0.1]:8001/v1', 'http://host.docker.internal:8001/v1'),
    ('https://127.0.0.1.example/v1', 'https://127.0.0.1.example/v1'),
    ('https://host.example/path/localhost', 'https://host.example/path/localhost'),
])
def test_gateway_rewrites_only_docker_loopback(url, rewritten):
    assert gateway_base_url(url, docker=True) == rewritten
    assert gateway_base_url(url, docker=False) == url.rstrip('/')


@pytest.mark.parametrize('url', ['https://a:b@example.invalid/v1', 'https://example.invalid/v1?k=x',
                                 'https://example.invalid/v1#k', 'file:///tmp/model'])
def test_provider_url_excludes_inline_credentials(url):
    with pytest.raises(ValidationError):
        local_provider(base_url=url)


@pytest.mark.parametrize('name', ['deepseek-flash', 'DeepSeek-V4-Pro', 'all-proxy-models', 'all-team-models'])
def test_gateway_reserves_preset_and_key_policy_names(name):
    with pytest.raises(RegistryError):
        ProviderRegistry().with_provider(uuid4(), local_provider(models=[{'name': name, 'upstream_model': 'm'}]))


def test_local_default_prefers_available_model_and_preserves_deepseek(tmp_path, monkeypatch):
    env = tmp_path / 'fixture.env'
    monkeypatch.setenv('SCA_PORTAL_LOCAL_CONFIG', str(env))
    ProviderRegistry().with_provider(uuid4(), local_provider()).save(tmp_path)
    assert local_default_model(tmp_path, 'deepseek-flash') == 'local-coder'
    env.write_text('DEEPSEEK_API_KEY=fixture-provider-key\n', encoding='utf-8')
    assert local_default_model(tmp_path, 'deepseek-flash') == 'deepseek-flash'
    assert local_default_model(tmp_path, 'local-coder') == 'local-coder'


@pytest.mark.parametrize(('protocol', 'wire'), [('messages', 'anthropic-messages'), ('openai', 'openai-completions')])
def test_dsh_declares_gateway_alias_with_context_window(protocol, wire):
    config = json.loads(render_patch('http://gateway/v1', 'local-coder', protocol, registered=True,
                                     context_window=8192))
    rows = {row['id']: row for row in config}
    profile = rows['llm-pi-ai']['config']['providers']['sca-gateway']
    assert rows['llm-deepseek']['disabled']
    assert profile['api'] == wire
    assert profile['baseURL'] == ('http://gateway' if protocol == 'messages' else 'http://gateway/v1')
    assert profile['models'] == [{'id': 'local-coder', 'contextWindow': 8192, 'maxTokens': 2048}]
    assert rows['agent-default-model']['config']['model'] == 'local-coder'
