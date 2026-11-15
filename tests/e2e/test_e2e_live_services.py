"""Phase 2 components checked against the real open-source services they are built on.

Needs: docker compose --profile s3 --profile oidc up -d
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from sca.api.auth import OidcAuthenticator
from sca.core.errors import AuthError
from sca.storage.s3 import S3ObjectStore

from .conftest import sandbox_running

pytestmark = [pytest.mark.e2e, pytest.mark.live]

S3_URL = "http://localhost:8333"
DEX = "http://localhost:5556/dex"


def _up(url: str) -> bool:
    try:
        httpx.get(url, timeout=3)
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture
def agent_backend() -> str:
    return "fake"


@pytest.fixture
def settings_overrides():
    if not _up(S3_URL):
        pytest.skip("S3 service is not running")
    bucket = f"sca-e2e-{uuid.uuid4().hex[:8]}"
    yield {
        "object_store": "s3",
        "s3_endpoint_url": S3_URL,
        "s3_bucket": bucket,
        "s3_access_key": "any",
        "s3_secret_key": "any",
    }
    # SeaweedFS allocates volumes per bucket; leftover test buckets exhaust them.
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=S3_URL,
        aws_access_key_id="any",
        aws_secret_access_key="any",
        region_name="us-east-1",
    )
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])
    client.delete_bucket(Bucket=bucket)


async def test_full_task_with_artifacts_in_s3(stack, source_repo):
    fix = "printf 'def add(a, b):\\n    return a + b\\n' > calc.py"
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n```sca-fake\n{fix}\n```",
            "verify_command": "python -m pytest -q",
        }
    )
    await stack.wait_for(task_id, "AWAITING_REVIEW")
    # The patch is served by the API straight from the S3 bucket.
    assert "+    return a + b" in await stack.patch(task_id)
    artifacts = (await stack.http.get(f"/v1/tasks/{task_id}/artifacts")).json()
    assert artifacts and all(a["uri"].startswith("s3://") for a in artifacts)
    s3 = S3ObjectStore(
        stack.settings.s3_bucket, endpoint_url=S3_URL, access_key="any", secret_key="any"
    )
    assert f"tasks/{task_id}/changes.patch" in await s3.list(f"tasks/{task_id}/")
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
    assert not sandbox_running(task_id)


async def test_dex_issued_token_is_accepted():
    if not _up(f"{DEX}/.well-known/openid-configuration"):
        pytest.skip("Dex is not running")
    async with httpx.AsyncClient(timeout=10) as http:
        conf = (await http.get(f"{DEX}/.well-known/openid-configuration")).json()
        r = await http.post(
            conf["token_endpoint"],
            data={
                "grant_type": "password",
                "username": "dev@example.com",
                "password": "password",
                "scope": "openid email",
            },
            auth=("sca-cli", "sca-cli-secret"),
        )
        assert r.status_code == 200, r.text
        token = r.json()["id_token"]

    auth = OidcAuthenticator(DEX, "sca-cli", conf["jwks_uri"], tenant_claim="email")
    principal = await auth.authenticate(token)
    assert principal.tenant_id == "dev@example.com" and principal.subject

    with pytest.raises(AuthError):
        await auth.authenticate(token[:-6] + "AAAAAA")
    with pytest.raises(AuthError):
        await OidcAuthenticator(DEX, "another-client", conf["jwks_uri"], "email").authenticate(
            token
        )
