"""Registry, grants, migration RLS, and pgvector against Postgres."""

import socket
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from sca.api.app import create_app
from sca.config import Settings, get_settings
from sca.core.errors import AuthError, NotFoundError
from sca.core.models import Principal
from sca.db.schema import kb_chunks, kb_documents, tool_grants, tools
from sca.knowledge.models import DocumentInput
from sca.knowledge.service import ingest, search
from sca.storage.local import LocalObjectStore
from sca.tools.settings import ToolSettings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")


class Authenticator:
    async def authenticate(self, token):
        identities = {
            "admin": Principal(tenant_id="company", subject="admin"),
            "alice": Principal(tenant_id="company", subject="alice", role="member"),
            "bob": Principal(tenant_id="company", subject="bob", role="viewer"),
            "another": Principal(tenant_id="another", subject="alice"),
            "platform": Principal(tenant_id="platform", subject="operator"),
            "ci": Principal(tenant_id="company", subject="ci", scopes=["ci:report"]),
        }
        if token not in identities:
            raise AuthError("Invalid token")
        return identities[token]


@pytest.fixture
async def api(api_store, tmp_path, monkeypatch):
    settings = ToolSettings(tool_teams={"company": {"alice": ["ops"]}})
    monkeypatch.setattr("sca.api.tools.get_tool_settings", lambda: settings)
    monkeypatch.setattr("sca.api.knowledge.get_tool_settings", lambda: settings)
    monkeypatch.setattr("sca.api.knowledge.get_settings", lambda: Settings(_env_file=None))
    app = create_app(store=api_store, object_store=LocalObjectStore(tmp_path),
                     authenticator=Authenticator())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def request(method, path, token="admin", **kwargs):
            return await client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

        yield request


async def test_registry_crud_and_user_team_tenant_grants(api):
    root, path = "/v1/tools", "/v1/tools/tickets"
    tool = {"name": "tickets", "url": "https://tickets.example.org/mcp",
            "headers": {"Authorization": "tickets-auth"}}
    assert (await api("POST", root, "alice", json=tool)).status_code == 403
    response = await api("POST", root, json=tool)
    assert response.status_code == 201
    assert response.json()["version"] == 1 and response.json()["created_by"] == "admin"
    assert (await api("POST", root, json=tool)).status_code == 409
    assert (await api("GET", path, "another")).status_code == 404
    assert (await api("GET", f"{root}/available", "ci")).status_code == 403

    async def available(token):
        return {tool["name"] for tool in (await api("GET", f"{root}/available", token)).json()}

    assert await available("alice") == set()
    for subject_type, subject in (("user", "bob"), ("team", "ops")):
        grant = {"subject_type": subject_type, "subject": subject}
        for _ in range(2):
            assert (await api("POST", path + "/grants", json=grant)).status_code == 201
    assert await available("alice") == await available("bob") == {"tickets"}
    assert await available("admin") == await available("another") == set()
    grants = (await api("GET", path + "/grants")).json()
    assert [(g["subject_type"], g["granted_by"]) for g in grants] == [("team", "admin"), ("user", "admin")]
    assert (await api("DELETE", path + "/grants?subject_type=user&subject=bob")).status_code == 204
    assert await available("bob") == set()
    grant = {"subject_type": "tenant", "subject": "company"}
    assert (await api("POST", path + "/grants", json=grant)).status_code == 201
    assert await available("bob") == {"tickets"}
    patched = await api("PATCH", path, json={"enabled": False})
    assert patched.json()["version"] == 2
    assert await available("alice") == set()
    assert (await api("DELETE", path)).status_code == 204
    assert (await api("GET", path)).status_code == 404


async def test_platform_visibility_and_write_policies(api, api_store):
    root, path = "/v1/tools", "/v1/tools/shared"
    spec = {"name": "shared", "url": "https://shared.example.org/mcp"}
    assert (await api("POST", root, "platform", json=spec)).status_code == 201
    assert (await api("GET", path)).json()["tenant_id"] == "platform"
    assert (await api("PATCH", path, json={"enabled": False})).status_code == 404
    assert (await api("GET", f"{root}/available", "alice")).json() == []
    grant = {"subject_type": "tenant", "subject": "company"}
    assert (await api("POST", path + "/grants", json=grant)).status_code == 403
    assert (await api("POST", path + "/grants", "platform",
                      json={**grant, "tenant_id": "company"})).status_code == 201
    assert (await api("GET", path + "/grants?tenant_id=company", "platform")).json()[0]["subject"] == "company"
    assert (await api("POST", f"{root}/knowledge/grants", json=grant)).status_code == 201
    assert {r["name"] for r in (await api("GET", f"{root}/available", "alice")).json()} == {
        "knowledge", "shared",
    }
    assert (await api("POST", root, json={**spec, "enabled": False})).status_code == 201
    assert [r["name"] for r in (await api("GET", f"{root}/available", "alice")).json()] == ["knowledge"]
    assert (await api("DELETE", path)).status_code == 204
    assert (await api("GET", path)).json()["tenant_id"] == "platform"

    other = api_store.for_tenant("another")
    async with other._transaction() as session:
        assert set((await session.scalars(select(tools.c.name))).all()) == {"knowledge", "shared"}
        assert not (await session.execute(select(tool_grants))).all()
        assert (await session.execute(update(tools).where(tools.c.name == "shared")
                                      .values(enabled=False))).rowcount == 0
        assert (await session.execute(delete(tools).where(tools.c.name == "shared"))).rowcount == 0
    with pytest.raises(DBAPIError):
        async with other._transaction() as session:
            await session.execute(insert(tool_grants).values(
                tenant_id="another", tool_tenant_id="platform", tool_name="shared",
                subject_type="tenant", subject="another", granted_by="intruder",
            ))
    with pytest.raises(DBAPIError):
        async with other._transaction() as session:
            await session.execute(insert(tools).values(
                url=spec["url"], name="other-shared", tenant_id="platform", description="", kind="remote",
                created_by="intruder",
            ))
    revoke = path + "/grants?subject_type=tenant&subject=company&tenant_id=company"
    assert (await api("DELETE", revoke, "platform")).status_code == 204
    assert [r["name"] for r in (await api("GET", f"{root}/available", "alice")).json()] == ["knowledge"]


async def test_knowledge_http_acl_and_chunk_cascade(api, api_store):
    path = "/v1/knowledge/documents"
    payload = {"title": "Runbook", "format": "markdown", "text": "## Rotation\n\nReplace keys.",
               "allowed_teams": ["ops"]}
    assert (await api("POST", path, "alice", json=payload)).status_code == 403
    created = await api("POST", path, json=payload)
    assert created.status_code == 201
    doc_id = UUID(created.json()["id"])
    assert (await api("GET", path, "bob")).json() == []
    assert (await api("GET", path, "alice")).json()[0]["id"] == str(doc_id)
    for token, count in (("bob", 0), ("another", 0), ("alice", 1)):
        hits = await api("POST", "/v1/knowledge/search", token, json={"query": "replace keys", "k": 1})
        assert len(hits.json()) == count
    with pytest.raises(NotFoundError):
        await api_store.for_tenant("company").get_knowledge_document(doc_id, [])
    async with api_store.for_tenant("another")._transaction() as session:
        assert not (await session.execute(select(kb_documents))).all()
        assert not (await session.execute(select(kb_chunks))).all()
    assert (await api("DELETE", f"{path}/{doc_id}", "another")).status_code == 404
    assert (await api("DELETE", f"{path}/{doc_id}")).status_code == 204
    async with api_store.for_tenant("company")._transaction() as session:
        assert not (await session.execute(select(kb_chunks))).all()


async def test_pgvector_ranks_eligible_chunks_with_fake_embedder(api_store):
    db = api_store.for_tenant("company")
    async with db._transaction() as session:
        if not await db._has_knowledge_vectors(session):
            pytest.skip("pgvector extension unavailable in test database")

    class Embedder:
        async def embed(self, texts, model):
            assert model == "fake-embedder"
            vectors = {"Expiry": [1.0, 0.0], "Notes": [0.5, 0.5], "Control": [0.0, 1.0],
                       "retention": [1.0, 0.0]}
            return [vectors[text.splitlines()[0]] for text in texts]

    settings = Settings(_env_file=None, memory_embedding_model="fake-embedder")
    for title, teams in (("Control", []), ("Notes", []), ("Expiry", ["ops"])):
        await ingest(db, DocumentInput(title=title, text="A seven day policy", allowed_teams=teams),
                     "admin", settings, Embedder())

    for teams, expected in ((["ops"], "Expiry"), ([], "Notes")):
        hits = await search(db, teams, "retention", 1, settings, Embedder())
        assert [hit.title for hit in hits] == [expected]
        assert hits[0].citation.endswith("#chunk-1")
        assert hits[0].score is not None and hits[0].score > 0.7
    assert await search(api_store.for_tenant("another"), ["ops"], "retention", 1,
                        settings, Embedder()) == []
