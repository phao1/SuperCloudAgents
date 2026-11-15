"""Real sandbox intent acceptance; requires the optional sca-sandbox-web image."""

import json
import shlex
import shutil
import subprocess

import pytest

from .conftest import git

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
IMAGE = "sca-sandbox-web:latest"


@pytest.fixture(autouse=True)
def web_image():
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker unavailable")
    if result.returncode:
        pytest.skip("Docker or sca-sandbox-web image unavailable")


@pytest.fixture
def settings_overrides():
    return {"sandbox_image": IMAGE}


@pytest.fixture
def agent_backend(request):
    return "codex" if request.node.get_closest_marker("codex") else "fake"


@pytest.fixture
def intent_repo(tmp_path, web_image):
    seed = tmp_path / "intent-seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "server.py").write_text("""from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = 'body.json' if self.path == '/item' else 'index.html'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if path == 'body.json' else 'text/html')
        self.end_headers()
        self.wfile.write(Path(path).read_bytes())
    def _not_allowed(self):
        # Like any real web framework: a method the API does not offer is 405, not 501.
        self.send_response(405)
        self.send_header('Allow', 'GET')
        self.send_header('Content-Length', '0')
        self.end_headers()
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = _not_allowed
HTTPServer(('127.0.0.1', 8123), Handler).serve_forever()
""")
    (seed / "body.json").write_text("{}")
    (seed / "index.html").write_text(
        '<html><body><button style="background:red">Go</button></body></html>'
    )
    (seed / "code.py").write_text("import os\n")
    (seed / "ruff.toml").write_text('[lint]\nselect = ["F401"]\n')
    schema = {
        "openapi": "3.0.3",
        "info": {"title": "Items", "version": "1"},
        "paths": {
            "/item": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "item",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["name"],
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    (seed / "openapi.json").write_text(json.dumps(schema))
    html = '<html><body><button style="background:blue">Go</button></body></html>'
    script = (
        "const {chromium}=require('/opt/sca-web/node_modules/playwright');"
        "(async()=>{const b=await chromium.launch({headless:true});"
        "const p=await b.newPage({viewport:{width:1280,height:720}});"
        f"await p.setContent({json.dumps(html)});"
        "await p.screenshot({path:'/fixture/baseline.png'});await b.close()})()"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{seed}:/fixture",
            IMAGE,
            "node",
            "-e",
            script,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "fixture")
    bare = tmp_path / "intent-origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


def source(adapter):
    service = {
        "start_command": "python server.py",
        "base_url": "http://127.0.0.1:8123",
        "ready_url": "http://127.0.0.1:8123/item",
    }
    return {
        "type": adapter,
        "name": "design",
        "path": {"openapi": "openapi.json", "visual": "baseline.png", "rules": "ruff.toml"}[
            adapter
        ],
        "params": {
            "rules": {"tool": "ruff"},
            "openapi": service,
            "visual": {**service, "page_url": "http://127.0.0.1:8123"},
        }[adapter],
    }


def fix(adapter):
    filename, text = {
        "openapi": ("body.json", '{"name":"item"}'),
        "visual": (
            "index.html",
            '<html><body><button style="background:blue">Go</button></body></html>',
        ),
        "rules": ("code.py", ""),
    }[adapter]
    return "printf %s " + shlex.quote(text) + " > " + shlex.quote(filename)


@pytest.mark.parametrize("adapter", ["openapi", "visual", "rules"])
@pytest.mark.parametrize("repair", [False, True], ids=["passing", "fails-then-fixed"])
async def test_intent_fix_loop(stack, intent_repo, adapter, repair):
    scripts = (["true"] if repair else []) + [fix(adapter)]
    instruction = "Implement the intent.\n" + "\n".join(f"```sca-fake\n{s}\n```" for s in scripts)
    task_id = await stack.submit(
        {"repo_url": str(intent_repo), "instruction": instruction, "intents": [source(adapter)]}
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=300)
    assert task["result"]["verification_passed"]
    assert task["result"]["turns"] == (2 if repair else 1)
    events = await stack.events(task_id)
    checks = [e for e in events if e["type"] == "intents.checked"]
    assert [e["payload"]["checks"][0]["exit_code"] == 0 for e in checks] == (
        [False, True] if repair else [True]
    )
    assert sum(e["type"] == "intents.compiled" for e in events) == 1
    if adapter == "visual" and repair:
        response = await stack.http.get(f"/v1/tasks/{task_id}/artifacts/intent-design-turn1.png")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
        prompts = [e["payload"]["prompt"] for e in events if e["type"] == "agent.turn_started"]
        assert ".sca/context/visual-design/diff.png" in prompts[1]
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


@pytest.mark.codex
async def test_openapi_real_model(stack, intent_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(intent_repo),
            "instruction": "Implement the supplied intent specification.",
            "intents": [source("openapi")],
            "turn_timeout_s": 600,
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900)
    assert task["result"]["verification_passed"]
    assert "body.json" in await stack.patch(task_id)
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
