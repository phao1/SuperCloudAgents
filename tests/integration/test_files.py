import hashlib
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import get_settings
from sca.db.schema import files
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def file_api(api_store, tmp_path):
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        store=api_store,
        object_store=objects,
        authenticator=StaticTokenAuthenticator({"a": "one", "b": "two"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer a"},
    ) as client:
        yield client, objects


async def test_stream_upload_checksum_metadata_and_tenant_isolation(file_api, api_store):
    client, objects = file_api
    content = b"x,value\n1,2\n" * 20000
    body = (
        b'--test\r\nContent-Disposition: form-data; name="file"; filename="data.csv"\r\n\r\n'
        + content
        + b"\r\n--test--\r\n"
    )

    async def chunks():
        for offset in range(0, len(body), 113):
            yield body[offset : offset + 113]

    response = await client.post(
        "/v1/files",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["sha256"] == hashlib.sha256(content).hexdigest()
    assert row["size_bytes"] == len(content)
    assert await objects.get(f"tenants/one/files/{row['id']}") == content
    assert (await client.get("/v1/files/" + row["id"])).json() == row
    assert (
        await client.get("/v1/files/" + row["id"], headers={"Authorization": "Bearer b"})
    ).status_code == 404
    response = await client.post(
        "/v1/tasks",
        headers={"Authorization": "Bearer b"},
        json={"kind": "analysis", "spec": {"question": "Mean?", "inputs": [row["id"]]}},
    )
    assert response.status_code == 422
    async with api_store.sessionmaker.begin() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', 'two', true)"))
        assert not (await session.execute(select(files).where(files.c.id == UUID(row["id"])))).all()


async def test_limit_rejects_without_object_or_metadata(file_api, monkeypatch):
    client, objects = file_api
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    response = await client.post("/v1/files", files={"file": ("large.csv", b"x" * (1024**2 + 1))})
    assert response.status_code == 413, response.text
    assert await objects.list("tenants/") == []


async def test_malformed_and_multiple_files(file_api):
    client, _ = file_api
    assert (await client.post("/v1/files", content=b"bad")).status_code == 422
    response = await client.post("/v1/files", files=[("file", ("a", b"a")), ("file", ("b", b"b"))])
    assert response.status_code in {413, 422}


async def test_upload_roles(api_store, tmp_path):
    from sca.core.models import Principal

    class Roles:
        async def authenticate(self, token):
            return Principal(tenant_id="roles", subject="test", role=token)

    app = create_app(
        store=api_store, object_store=LocalObjectStore(tmp_path), authenticator=Roles()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/files", files={"file": ("a.csv", b"1")}, headers={"Authorization": "Bearer viewer"}
        )
        assert response.status_code == 403
        response = await client.post(
            "/v1/files", files={"file": ("a.csv", b"1")}, headers={"Authorization": "Bearer member"}
        )
        assert response.status_code == 201, response.text
        response = await client.get(
            "/v1/files/" + response.json()["id"], headers={"Authorization": "Bearer viewer"}
        )
        assert response.status_code == 200


async def test_analysis_prepare_and_publish_committed_artifacts(store, tmp_path):
    from uuid import uuid4

    from sca.agents.fake import FakeBackend
    from sca.config import Settings
    from sca.kernel.activities import PublishInput
    from sca.kinds.analysis.activities import AnalysisActivities

    objects = LocalObjectStore(tmp_path / "objects")
    file_id = uuid4()
    await store.for_tenant("one").create_file(
        file_id, "sample.csv", 10, hashlib.sha256(b"value\n1\n3\n").hexdigest()
    )
    await objects.put(f"tenants/one/files/{file_id}", b"value\n1\n3\n")
    task, _ = await store.create_task(
        tenant_id="one",
        kind="analysis",
        spec={"question": "Mean?", "inputs": [str(file_id)]},
        idempotency_key=None,
    )
    acts = AnalysisActivities(
        settings=Settings(data_dir=tmp_path / "data", repo_cache=False),
        store=store,
        sandbox=None,
        agent=FakeBackend(),
        objects=objects,
    )
    info = await acts.prepare(str(task.id))
    ws = acts._workspace(str(task.id))
    assert (ws.workspace / "data/sample.csv").read_bytes() == b"value\n1\n3\n"
    assert (ws.workspace / "data/sample.csv").stat().st_mode & 0o222 == 0
    assert (ws.workspace / ".gitattributes").read_text() == "data/** binary\n"
    assert (
        info.base_sha == (await ws._git("--git-dir", str(ws.mirror), "rev-parse", "main")).strip()
    )
    # This checkout has never entered a sandbox; all files here are trusted test fixtures.
    (ws.workspace / "output/figures").mkdir(parents=True)
    for path, data in [
        ("analysis.py", b"print(2)"),
        ("output/report.md", b"Mean: 2"),
        ("output/results.json", b'{"mean":2}'),
        ("output/figures/a.png", b"png"),
    ]:
        (ws.workspace / path).write_bytes(data)
    (ws.workspace / "output/figures/escape").symlink_to("/etc/passwd")
    await ws._git("-C", str(ws.workspace), "add", ".")
    await ws._git(
        "-C",
        str(ws.workspace),
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "trusted fixture",
    )
    commit = await ws.fetch()
    await store.update_result(task.id, {"commit": commit})
    result = await acts.publish(PublishInput(str(task.id), 1, True))
    assert set(result["artifacts"]) == {"analysis.py", "report.md", "results.json", "figures/a.png"}
    assert await objects.get(f"tasks/{task.id}/report.md") == b"Mean: 2"
