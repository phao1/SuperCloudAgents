"""Optional wire test using an installed LiteLLM proxy and a tiny local upstream."""

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
import yaml

from sca.gateway.model_providers import ProviderInput, ProviderRegistry, gateway_config

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_litellm_routes_chat_messages_and_responses_to_openai_compatible_upstream(tmp_path):
    if find_spec('litellm') is None:
        pytest.skip('LiteLLM proxy is not installed in the test venv')
    try:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            proxy_port = listener.getsockname()[1]
    except PermissionError:
        pytest.skip('Sandbox denies loopback sockets')

    requests = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['content-length'])))
            requests.append((self.path, body))
            payload = json.dumps({
                'id': 'chatcmpl-local', 'object': 'chat.completion', 'created': 1,
                'model': body['model'], 'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5},
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}],
            }).encode()
            status = 200 if self.path == '/v1/chat/completions' else 404
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Upstream) as upstream:
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ProviderInput(
                name='Fake OpenAI-compatible upstream', type='openai-compatible',
                base_url=f'http://127.0.0.1:{upstream.server_port}/v1',
                models=[{'name': 'local', 'upstream_model': 'org/upstream-test'}],
            )
            registry = ProviderRegistry().with_provider(uuid4(), provider)
            generated = gateway_config(ROOT, tmp_path, registry, deepseek=False, docker=False)
            config = yaml.safe_load(generated.read_text(encoding='utf-8'))
            config['general_settings'].pop('database_url')
            generated.write_text(yaml.safe_dump(config), encoding='utf-8')
            verify_proxy(generated, proxy_port)
        finally:
            upstream.shutdown()
            thread.join(timeout=5)
    assert [path for path, _ in requests] == ['/v1/chat/completions'] * 3
    assert all(body['model'] == 'org/upstream-test' for _, body in requests)


def verify_proxy(config: Path, port: int) -> None:
    master = 'sk-local-fixture-master'
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update(LITELLM_MASTER_KEY=master, LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False')
    command = [sys.executable, '-c', 'from litellm.proxy.proxy_cli import run_server; run_server()',
               '--config', str(config), '--host', '127.0.0.1', '--port', str(port)]
    with subprocess.Popen(command, cwd=config.parent, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5) as client:
                deadline = monotonic() + 30
                while monotonic() < deadline and process.poll() is None:
                    try:
                        if client.get('/health/liveliness').is_success:
                            break
                    except httpx.TransportError:
                        pass
                    sleep(.1)
                else:
                    pytest.fail('LiteLLM did not start from the generated configuration')

                prompt = {'model': 'local', 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                          'max_tokens': 16}
                headers = {'Authorization': f'Bearer {master}'}
                chat = client.post('/v1/chat/completions', headers=headers, json=prompt)
                assert chat.status_code == 200
                assert chat.json()['choices'][0]['message']['content'] == 'OK'
                messages = client.post('/v1/messages', json=prompt, headers={
                    'x-api-key': master, 'anthropic-version': '2023-06-01',
                })
                assert messages.status_code == 200
                assert messages.json()['content'][0]['text'] == 'OK'
                responses = client.post('/v1/responses', headers=headers,
                                        json={'model': 'local', 'input': 'Reply OK.'})
                assert responses.status_code == 200
                assert responses.json()['output'][0]['content'][0]['text'] == 'OK'
        finally:
            process.kill()
            process.wait(timeout=10)
