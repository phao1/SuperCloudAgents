"""B4 private repository isolation and credential rotation (real Docker services)."""

from __future__ import annotations

import asyncio
import json
import mmap
import os
import subprocess
import tempfile
import uuid
from urllib.parse import urlsplit

import httpx
import hvac
import pytest
import requests
from temporalio.client import Client

from sca.core.models import RepoCredential
from sca.secrets import git_env

from .test_e2e_ci_real import GITEA, ROOT, _seed
from .test_e2e_fake import RIGHT_FIX, block

pytestmark = [pytest.mark.e2e, pytest.mark.live]


@pytest.fixture
def bao(request):
    if request.node.name == "test_github_app_real":
        directory = ROOT / ".data/github-app"
        if not os.getenv("SCA_E2E_GITHUB_APP_REPO") or not all(
            (directory / name).is_file() and (directory / name).read_text().strip()
            for name in ("private-key.pem", "app.json", "installation_id")
        ):
            pytest.skip(
                "Run github_app_setup manifest/convert/status and set SCA_E2E_GITHUB_APP_REPO"
            )
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=5)
        if request.node.name != "test_github_app_real":
            httpx.get(f"{GITEA['GITEA_URL']}/api/healthz", timeout=3).raise_for_status()
        client = hvac.Client(
            url="http://127.0.0.1:8200",
            token=os.getenv("OPENBAO_DEV_ROOT_TOKEN", "sca-dev-root"),
            timeout=3,
        )
        assert client.is_authenticated()
    except (
        OSError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        KeyError,
        hvac.exceptions.VaultError,
        requests.RequestException,
        AssertionError,
    ):
        pytest.skip("Docker and OpenBao are required (also Gitea for the Gitea test)")
    return client


@pytest.fixture
def settings_overrides(bao):
    return {"secret_provider": "openbao", "openbao_url": bao.url, "openbao_token": bao.token}


async def test_private_repository_credentials(stack, bao, tmp_path):
    owner = GITEA["GITEA_USER"]
    name = f"secrets-{uuid.uuid4().hex[:10]}"
    remote = f"{GITEA['GITEA_URL']}/{owner}/{name}.git"
    path = f"sca/tenants/default/repos/{urlsplit(remote).hostname}/{owner}/{name}"
    tokens = []
    async with httpx.AsyncClient(
        base_url=f"{GITEA['GITEA_URL']}/api/v1",
        headers={"Authorization": f"token {GITEA['GITEA_TOKEN']}"},
    ) as api:

        async def issue():
            response = await api.post(
                f"/users/{owner}/tokens",
                auth=(owner, GITEA["GITEA_PASSWORD"]),
                json={"name": f"b4-{uuid.uuid4().hex}", "scopes": ["write:repository"]},
            )
            response.raise_for_status()
            entry = response.json()
            tokens.append(entry)
            bao.secrets.kv.v2.create_or_update_secret(
                path=path, secret={"type": "token", "username": owner, "token": entry["sha1"]}
            )
            return entry["sha1"]

        async def run(token):
            task_id = await stack.submit(
                {
                    "repo_url": remote,
                    "instruction": block(RIGHT_FIX),
                    "verify_command": "python -m pytest -q",
                    "push": True,
                }
            )
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=180)
            assert task["result"]["pushed"]
            assert token not in json.dumps(await stack.events(task_id))
            client = await Client.connect(stack.settings.temporal_address)
            history = await client.get_workflow_handle(f"task-{task_id}").fetch_history()
            assert token not in history.to_json()
            directory = stack.settings.task_dir(task_id)
            assert token not in (directory / "mirror.git/config").read_text()
            sandbox = task["result"]["sandbox_id"]
            inspect = await asyncio.to_thread(
                subprocess.run, ["docker", "inspect", sandbox], check=True, capture_output=True
            )
            assert token.encode() not in inspect.stdout

            def scan_filesystem():
                with tempfile.TemporaryFile() as archive:
                    subprocess.run(
                        ["docker", "export", sandbox],
                        check=True,
                        stdout=archive,
                        stderr=subprocess.PIPE,
                    )
                    with mmap.mmap(archive.fileno(), 0, access=mmap.ACCESS_READ) as data:
                        assert data.find(token.encode()) == -1

            await asyncio.to_thread(scan_filesystem)
            scan = await asyncio.to_thread(
                subprocess.run,
                [
                    "docker",
                    "exec",
                    sandbox,
                    "sh",
                    "-c",
                    "tar cf - /workspace /agent_home 2>/dev/null",
                ],
                capture_output=True,
            )
            assert scan.returncode == 0
            assert token.encode() not in scan.stdout
            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")

        try:
            response = await api.post("/user/repos", json={"name": name, "private": True})
            response.raise_for_status()
            token = await issue()
            seed = _seed(tmp_path, ROOT / "deploy/ci/gitea-actions.yml", ".gitea/workflows/sca.yml")
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-c", "credential.helper=", "push", remote, "main"],
                cwd=seed,
                env=git_env(RepoCredential(username=owner, password=token)),
                check=True,
                capture_output=True,
            )
            await run(token)
            from dataclasses import replace

            from sca.api.app import create_app
            from sca.api.auth import StaticTokenAuthenticator
            from sca.orchestration.client import TemporalOrchestrator

            client = await Client.connect(stack.settings.temporal_address)
            app = create_app(
                store=stack.store,
                orchestrator=TemporalOrchestrator(client, stack.settings.task_queue),
                authenticator=StaticTokenAuthenticator({"other-token": "other"}),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://sca.test",
                headers={"Authorization": "Bearer other-token"},
            ) as http:
                other = replace(stack, http=http)
                other_id = await other.submit({"repo_url": remote, "instruction": "fix"})
                await other.wait_for(other_id, "FAILED")
                assert not any(
                    e["type"] == "workspace.prepared" for e in await other.events(other_id)
                )
                assert not (stack.settings.task_dir(other_id) / "mirror.git").exists()
            from sca.workspace.cache import RepoCache

            cache = RepoCache(stack.settings.data_dir / "cache/repos")
            assert not cache._path(remote, "other").exists()
            replacement = await issue()
            # Gitea manages access tokens only with the user's password, not with a token.
            revoked = await api.delete(
                f"/users/{owner}/tokens/{tokens[0]['id']}", auth=(owner, GITEA["GITEA_PASSWORD"])
            )
            assert revoked.status_code == 204
            await run(replacement)
        finally:
            bao.secrets.kv.v2.delete_metadata_and_all_versions(path=path)
            for entry in tokens:
                await api.delete(
                    f"/users/{owner}/tokens/{entry['id']}", auth=(owner, GITEA["GITEA_PASSWORD"])
                )
            await api.delete(f"/repos/{owner}/{name}")


@pytest.mark.live
async def test_github_app_real(stack, bao, monkeypatch, caplog, capsys):
    from sca.secrets import OpenBaoSecretProvider

    directory = ROOT / ".data/github-app"
    repo = os.environ["SCA_E2E_GITHUB_APP_REPO"]
    import re

    assert re.fullmatch(r"[\w.-]+/[\w.-]+", repo), "Expected owner/repo"
    remote = f"https://github.com/{repo}"
    metadata = json.loads((directory / "app.json").read_text())
    entry = {
        "type": "github_app",
        "app_id": metadata["app_id"],
        "private_key": (directory / "private-key.pem").read_text(),
        "installation_id": (directory / "installation_id").read_text().strip(),
    }
    provider = OpenBaoSecretProvider(stack.settings)
    path = f"{provider.prefix}/default/repos/github.com/{repo}"
    tokens = []
    original = OpenBaoSecretProvider._installation

    async def record(self, data):
        credential = await original(self, data)
        tokens.append(credential.password)
        return credential

    monkeypatch.setattr(OpenBaoSecretProvider, "_installation", record)
    # Restore credentials already provisioned by the setup script after the test.
    try:
        previous = bao.secrets.kv.v2.read_secret_version(
            path=path, mount_point=provider.mount, raise_on_deleted_version=True
        )["data"]["data"]
    except hvac.exceptions.InvalidPath:
        previous = None
    bao.secrets.kv.v2.create_or_update_secret(path=path, secret=entry, mount_point=provider.mount)
    task_id = None
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=30) as api:
        try:
            credential = await provider.repo_credential("default", remote)
            headers = {"Authorization": f"Bearer {credential.password}"}
            response = await api.get(f"/repos/{repo}", headers=headers)
            response.raise_for_status()
            assert response.json()["private"], "Use a private fixture repository"
            task_id = await stack.submit(
                {
                    "repo_url": remote,
                    "base_ref": response.json()["default_branch"],
                    "instruction": block("printf 'GitHub App e2e\\n' > sca-github-app-e2e.txt"),
                    "verify_command": "test -s sca-github-app-e2e.txt",
                    "push": True,
                }
            )
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=180)
            assert task["result"]["pushed"]
            branch = task["result"]["branch"]
            response = await api.get(f"/repos/{repo}/git/ref/heads/{branch}", headers=headers)
            response.raise_for_status()
            assert response.json()["object"]["sha"] == task["result"]["commit"]
            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")
            history = (
                await (await Client.connect(stack.settings.temporal_address))
                .get_workflow_handle(f"task-{task_id}")
                .fetch_history()
            )
            captured = capsys.readouterr()
            texts = [
                json.dumps(await stack.events(task_id)),
                caplog.text,
                captured.out,
                captured.err,
                history.to_json(),
            ]
            assert len(tokens) >= 2, "The worker must acquire its own installation token"
            for token in tokens:
                assert all(token not in text for text in texts), "Installation token leaked"
        finally:
            try:
                if task_id:
                    task = await stack.task(task_id)
                    branch = (task.get("result") or {}).get("branch")
                    if branch:
                        response = await api.delete(
                            f"/repos/{repo}/git/refs/heads/{branch}", headers=headers
                        )
                        if response.status_code != 204:
                            check = await api.get(
                                f"/repos/{repo}/git/ref/heads/{branch}", headers=headers
                            )
                            assert check.status_code == 404, "Branch cleanup failed"
            finally:
                if previous is None:
                    bao.secrets.kv.v2.delete_metadata_and_all_versions(
                        path=path, mount_point=provider.mount
                    )
                else:
                    bao.secrets.kv.v2.create_or_update_secret(
                        path=path, secret=previous, mount_point=provider.mount
                    )
