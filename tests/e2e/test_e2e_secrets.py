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
def bao():
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=5)
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
        pytest.skip("Docker, Gitea and OpenBao are required")
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
