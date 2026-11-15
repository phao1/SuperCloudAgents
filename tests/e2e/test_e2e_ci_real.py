"""B3: a real CI system runs the project's pipeline on the pushed branch and reports back.

Gitea (compose profile `gitea`, set up by deploy/gitea/setup.sh) always; GitHub when a
self-hosted runner for the private fixture repository is running (deploy/ci/github-runner.sh) and
the gh token has the `workflow` scope.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
import requests
import uvicorn
from temporalio.client import Client

from sca.api.app import create_app
from sca.api.auth import ChainAuthenticator, DatabaseTokenAuthenticator, StaticTokenAuthenticator
from sca.orchestration.client import TemporalOrchestrator
from sca.storage import make_object_store

from .conftest import git
from .test_e2e_fake import RIGHT_FIX, block

pytestmark = [pytest.mark.e2e, pytest.mark.live]

ROOT = Path(__file__).resolve().parents[2]


def _env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


async def _serve_api(stack) -> tuple[uvicorn.Server, asyncio.Task, str]:
    """The real HTTP API, so a CI runner outside this process can call it."""
    client = await Client.connect(
        stack.settings.temporal_address, namespace=stack.settings.temporal_namespace
    )
    app = create_app(
        store=stack.store,
        orchestrator=TemporalOrchestrator(client, stack.settings.task_queue),
        authenticator=ChainAuthenticator([
            DatabaseTokenAuthenticator(stack.store),
            StaticTokenAuthenticator({"e2e-token": "default"}),
        ]),
        object_store=make_object_store(stack.settings),
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(30):
        while not server.started:
            await asyncio.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}"


async def _ci_token(api_url: str, repo_url: str) -> str:
    async with httpx.AsyncClient(base_url=api_url) as client:
        response = await client.post('/v1/tokens',
            headers={'Authorization': 'Bearer e2e-token'},
            json={'name': 'real-ci', 'scopes': ['ci:report'], 'repo_url': repo_url})
        assert response.status_code == 201
        token = response.json()['token']
        denied = await client.get('/v1/tasks', headers={'Authorization': f'Bearer {token}'})
        assert denied.status_code == 403
        return token


def _seed(workdir: Path, pipeline: Path, pipeline_path: str) -> Path:
    seed = workdir / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (seed / "test_calc.py").write_text(
        "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
    )
    (seed / "ci").mkdir()
    shutil.copy(ROOT / "deploy/ci/report.sh", seed / "ci/report.sh")
    target = seed / pipeline_path
    target.parent.mkdir(parents=True)
    shutil.copy(pipeline, target)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial")
    return seed


async def _drive(stack, repo_url: str, run_url_prefix: str) -> None:
    """Round 1: CI fails on a rule local verification does not know. Round 2: it passes."""
    task_id = await stack.submit(
        {
            "repo_url": repo_url,
            "instruction": (
                f"Fix add.\n{block(RIGHT_FIX)}\n{block('echo fixed add > CHANGELOG.md')}"
            ),
            "verify_command": "python -m pytest -q",
            "ci": {"timeout_s": 600},
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900)
    assert task["result"]["verification_passed"] is True, task
    assert task["result"]["turns"] == 2
    events = await stack.events(task_id)
    ci = [e["payload"] for e in events if e["type"] == "ci.completed"]
    print("\nCI results:", [(c["passed"], c["url"]) for c in ci])
    assert [c["passed"] for c in ci] == [False, True]
    assert not any(c["timed_out"] for c in ci)
    assert "CHANGELOG.md is missing" in ci[0]["summary"]
    assert all((c["url"] or "").startswith(run_url_prefix) for c in ci)
    requested = [e["payload"]["commit"] for e in events if e["type"] == "ci.requested"]
    assert [c["commit"] for c in ci] == requested  # each result is about the commit pushed
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


# ---------------------------------------------------------------------------------- Gitea

GITEA = _env_file(ROOT / ".data/gitea/env")


@pytest.fixture
def gitea() -> dict[str, str]:
    if not GITEA:
        pytest.skip("Gitea is not set up (deploy/gitea/setup.sh)")
    try:
        httpx.get(f"{GITEA['GITEA_URL']}/api/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Gitea is not running")
    return GITEA


@pytest.fixture
def settings_overrides():
    try:
        import hvac

        client = hvac.Client(
            url="http://127.0.0.1:8200",
            token=os.getenv("OPENBAO_DEV_ROOT_TOKEN", "sca-dev-root"),
            timeout=2,
        )
        if client.is_authenticated():
            return {
                "secret_provider": "openbao",
                "openbao_url": client.url,
                "openbao_token": client.token,
            }
    except (httpx.HTTPError, OSError, hvac.exceptions.VaultError, requests.RequestException):
        return {}
    return {}


@pytest.fixture
def gitea_credentials(gitea, monkeypatch, settings_overrides):
    """Prefer OpenBao; keep host helpers only when the optional service is absent."""
    if settings_overrides:
        from urllib.parse import urlsplit

        import hvac

        client = hvac.Client(
            url=settings_overrides["openbao_url"], token=settings_overrides["openbao_token"]
        )
        path = f"sca/tenants/default/repos/{urlsplit(gitea['GITEA_URL']).hostname}/{gitea['GITEA_USER']}"
        client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret={
                "type": "token",
                "username": gitea["GITEA_USER"],
                "token": gitea["GITEA_TOKEN"],
            },
        )
        yield
        client.secrets.kv.v2.delete_metadata_and_all_versions(path=path)
        return
    helper = (
        f"!f() {{ echo username={gitea['GITEA_USER']}; echo password={gitea['GITEA_TOKEN']}; }}; f"
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"credential.{gitea['GITEA_URL']}.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", helper)
    yield


async def test_gitea_actions_drive_the_task(stack, gitea, gitea_credentials, tmp_path):
    api = httpx.AsyncClient(
        base_url=f"{gitea['GITEA_URL']}/api/v1",
        headers={"Authorization": f"token {gitea['GITEA_TOKEN']}"},
        timeout=30,
    )
    name = f"ci-{uuid.uuid4().hex[:8]}"
    owner = gitea["GITEA_USER"]
    server, serving, api_url = await _serve_api(stack)
    try:
        r = await api.post("/user/repos", json={"name": name, "private": True})
        assert r.status_code == 201, r.text
        repo_url = f"{gitea['GITEA_URL']}/{owner}/{name}.git"
        token = await _ci_token(api_url, repo_url)
        for secret, value in {"SCA_API_URL": api_url, "SCA_CI_TOKEN": token}.items():
            r = await api.put(
                f"/repos/{owner}/{name}/actions/secrets/{secret}", json={"data": value}
            )
            assert r.status_code in (201, 204), r.text
        repo_url = f"{gitea['GITEA_URL']}/{owner}/{name}.git"
        seed = _seed(tmp_path, ROOT / "deploy/ci/gitea-actions.yml", ".gitea/workflows/sca.yml")
        from sca.core.models import RepoCredential
        from sca.secrets import git_env

        await asyncio.to_thread(
            subprocess.run,
            ["git", "-c", "credential.helper=", "push", "-q", repo_url, "main"],
            cwd=seed,
            env=git_env(RepoCredential(username=owner, password=gitea["GITEA_TOKEN"])),
            check=True,
            capture_output=True,
        )

        await _drive(stack, repo_url, f"{gitea['GITEA_URL']}/{owner}/{name}/actions/runs/")
    finally:
        await api.delete(f"/repos/{owner}/{name}")
        await api.aclose()
        server.should_exit = True
        await serving


# --------------------------------------------------------------------------------- GitHub

GITHUB_REPO = os.environ.get("SCA_GITHUB_CI_REPO", "phao1/sca-ci-fixture")


def _gh(*args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, input=input
    ).stdout.strip()


@pytest.fixture
def github_runner():
    if not shutil.which("gh"):
        pytest.skip("gh is not installed")
    try:
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
        status = auth.stdout + auth.stderr
        if "workflow" not in status:
            pytest.skip("gh token lacks the `workflow` scope (gh auth refresh -s workflow)")
        runners = json.loads(_gh("api", f"repos/{GITHUB_REPO}/actions/runners"))["runners"]
    except subprocess.CalledProcessError as e:
        pytest.skip(f"cannot query {GITHUB_REPO}: {e.stderr}")
    if not any(
        r["status"] == "online" and any(lb["name"] == "sca-ci" for lb in r["labels"])
        for r in runners
    ):
        pytest.skip(f"no online self-hosted runner for {GITHUB_REPO} (deploy/ci/github-runner.sh)")
    if json.loads(_gh("api", f"repos/{GITHUB_REPO}"))["private"] is not True:
        pytest.skip("refusing to use a self-hosted runner with a public repository")


@pytest.fixture
def github_credentials(github_runner, settings_overrides):
    """With OpenBao the worker never falls back to the host's gh identity: seed the repo entry."""
    if not settings_overrides:
        yield
        return
    import hvac

    client = hvac.Client(
        url=settings_overrides["openbao_url"], token=settings_overrides["openbao_token"]
    )
    path = f"sca/tenants/default/repos/github.com/{GITHUB_REPO}"
    client.secrets.kv.v2.create_or_update_secret(
        path=path,
        secret={"type": "token", "username": "x-access-token", "token": _gh("auth", "token")},
    )
    yield
    client.secrets.kv.v2.delete_metadata_and_all_versions(path=path)


async def test_github_actions_drive_the_task(stack, github_runner, github_credentials, tmp_path):
    server, serving, api_url = await _serve_api(stack)
    remote = f"https://github.com/{GITHUB_REPO}.git"
    try:
        token = await _ci_token(api_url, remote)
        for secret, value in {"SCA_API_URL": api_url, "SCA_CI_TOKEN": token}.items():
            _gh("secret", "set", secret, "--repo", GITHUB_REPO, input=value)
        seed = _seed(tmp_path, ROOT / "deploy/ci/github-actions.yml", ".github/workflows/sca.yml")
        git(seed, "push", "-q", "--force", remote, "main")
        await _drive(stack, remote, f"https://github.com/{GITHUB_REPO}/actions/runs/")
    finally:
        for ref in json.loads(_gh("api", f"repos/{GITHUB_REPO}/branches")):
            if ref["name"].startswith("sca/"):
                _gh("api", "-X", "DELETE", f"repos/{GITHUB_REPO}/git/refs/heads/{ref['name']}")
        server.should_exit = True
        await serving
