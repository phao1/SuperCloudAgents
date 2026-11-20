from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import TaskState
from sca.knowledge.chunks import split_document
from sca.knowledge.models import DocumentInput
from sca.knowledge.service import ingest, search
from sca.storage.local import LocalObjectStore
from sca.tools.settings import ToolSettings
from sca.tools.tokens import PATH, issue_knowledge_token


def test_split_document_respects_sections_paragraphs_and_order():
    chunks = split_document("Intro\n\n## 安装\n\nFirst\n\nSecond\n\n### 检查\n\nVerify\n", limit=18)
    assert [(c.ordinal, c.heading, c.content) for c in chunks] == [
        (1, None, "Intro"), (2, "安装", "## 安装\n\nFirst"),
        (3, "安装", "Second"), (4, "检查", "### 检查\n\nVerify"),
    ]


@pytest.mark.parametrize("body", ["字" * 500, "word " * 110, "   \n\n"])
def test_split_document_bounds_long_paragraphs(body):
    chunks = split_document(body, limit=30)
    assert all(1 <= len(chunk.content) <= 30 for chunk in chunks)
    assert "".join("".join(chunk.content.split()) for chunk in chunks) == "".join(body.split())


class KnowledgeStore:
    sessionmaker = SimpleNamespace(kw={"bind": SimpleNamespace(url=make_url("postgresql://local/db"))})

    def __init__(self, tenant_id: str = "org", *, db=None):
        self.tenant_id = tenant_id
        self.db = db or SimpleNamespace(documents={}, tasks={}, grants={"org"}, vector=None)

    def for_tenant(self, tenant):
        return KnowledgeStore(tenant, db=self.db)

    async def get_task(self, task_id, tenant):
        task = self.db.tasks.get((self.tenant_id, task_id))
        if task is None or tenant != self.tenant_id:
            raise NotFoundError("Task not found")
        return task

    async def task_submitter(self, task_id):
        return (await self.get_task(task_id, self.tenant_id)).actor

    async def available_tools(self, actor, teams, *, names):
        assert names == ["knowledge"]
        return [{"name": "knowledge"}] if self.tenant_id in self.db.grants else []

    async def create_knowledge_document(self, document, chunks, embedding_model):
        doc_id = uuid4()
        row = {**document, "id": doc_id, "chunk_count": len(chunks), "created_at": datetime.now(UTC)}
        self.db.documents[self.tenant_id, doc_id] = (row, chunks, embedding_model)
        return row

    async def list_knowledge_documents(self, teams, limit, offset):
        rows = [row for row, _, _ in self.readable(teams)]
        return rows[offset:offset + limit]

    def readable(self, teams):
        return [(row, chunks, model) for (tenant, _), (row, chunks, model) in self.db.documents.items()
                if tenant == self.tenant_id and (not row["allowed_teams"]
                                                 or set(row["allowed_teams"]).intersection(teams))]

    async def search_knowledge(self, query, teams, limit, vector, model):
        self.db.vector = vector
        results = []
        for row, chunks, indexed_model in self.readable(teams):
            for chunk in chunks:
                embedding = chunk["embedding"] if model == indexed_model else None
                distance = None
                if vector and embedding:
                    distance = 1 - sum(q * v for q, v in zip(vector, embedding, strict=True))
                elif query.casefold() not in chunk["content"].casefold():
                    continue
                results.append({**chunk, "document_id": row["id"], "title": row["title"],
                                "distance": distance})
        results.sort(key=lambda row: row["distance"] if row["distance"] is not None else 2)
        return results[:limit]

    async def get_knowledge_document(self, document_id, teams):
        for row, chunks, _ in self.readable(teams):
            if row["id"] == document_id:
                return {**row, "citations": chunks}
        raise NotFoundError("知识库文档不存在")

    async def delete_knowledge_document(self, document_id):
        if self.db.documents.pop((self.tenant_id, document_id), None) is None:
            raise NotFoundError("知识库文档不存在")


class FakeEmbedder:
    async def embed(self, texts, model):
        assert model == "fake-embedding"
        return [[1.0, 0.0] if any(word in text for word in ("retention", "expires"))
                else [0.0, 1.0] for text in texts]


async def test_search_with_fake_embedder_filters_teams_and_returns_chunk_citations():
    settings = Settings(_env_file=None, memory_embedding_model="fake-embedding")
    store, gateway = KnowledgeStore(), FakeEmbedder()
    restricted = await ingest(store, DocumentInput(title="Retention", format="markdown",
        text="## Rotation\n\nA key expires after seven days.", allowed_teams=["ops"]), "admin",
        settings, gateway)
    await ingest(store, DocumentInput(title="Customers", text="Customer list."), "admin",
                 settings, gateway)
    results = await search(store, ["ops"], "retention", 1, settings, gateway)
    assert store.db.vector == [1.0, 0.0]
    hit, = results
    assert (hit.title, hit.heading, hit.score) == ("Retention", "Rotation", 1.0)
    assert hit.citation == f"{restricted.id}#chunk-1"
    assert {row.title for row in await search(store, [], "retention", 5, settings, gateway)} == {
        "Customers",
    }


@pytest.fixture
async def knowledge_api(tmp_path, monkeypatch):
    monkeypatch.setattr("sca.api.knowledge.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("sca.api.mcp.get_settings", lambda: Settings(_env_file=None))
    settings = ToolSettings(
        knowledge_mcp_signing_key="unit-test-signing-material" * 2,
        knowledge_mcp_url="https://sca.example.org/v1/mcp/knowledge",
        tool_teams={"org": {"static-token": ["ops"]}},
    )
    monkeypatch.setattr("sca.api.knowledge.get_tool_settings", lambda: settings)
    monkeypatch.setattr("sca.api.mcp.get_tool_settings", lambda: settings)
    store = KnowledgeStore()
    app = create_app(store=store, object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator({"admin": "org", "outsider": "other"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        yield client, store, settings


def _task(store, *, actor="static-token", tools=None, tenant="org"):
    task_id = uuid4()
    store.db.tasks[tenant, task_id] = SimpleNamespace(
        state=TaskState.RUNNING, spec={"tools": ["knowledge"] if tools is None else tools}, actor=actor,
    )
    return task_id


async def _upload(client, title, text, teams, *, token="admin"):
    response = await client.post("/v1/knowledge/documents", headers={"Authorization": f"Bearer {token}"},
                                 json={"title": title, "text": text, "format": "pdf-text",
                                       "allowed_teams": teams})
    assert response.status_code == 201
    return response.json()["id"]


async def _rpc(client, token, method, params=None, id_=1, headers=None):
    return await client.post(PATH, json={"jsonrpc": "2.0", "method": method,
                                         **({"id": id_} if id_ is not None else {}),
                                         **({"params": params} if params is not None else {})},
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json, text/event-stream", **(headers or {})})


async def test_mcp_initialize_list_and_call_respects_team_and_tenant(knowledge_api):
    client, store, settings = knowledge_api
    document = await _upload(client, "Runbook", "Procedure for rotating account keys", ["ops"])
    foreign = await _upload(client, "Other runbook", "Procedure for rotating keys", [], token="outsider")
    task_id = _task(store)
    token = issue_knowledge_token(settings, task_id, "org", 60)
    initialize = await _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18"})
    assert initialize.status_code == 200
    assert initialize.json()["result"]["protocolVersion"] == "2025-06-18"
    notified = await _rpc(client, token, "notifications/initialized", id_=None)
    assert notified.status_code == 202 and not notified.content
    catalog = (await _rpc(client, token, "tools/list", id_="catalog")).json()
    assert catalog["id"] == "catalog"
    assert {tool["name"] for tool in catalog["result"]["tools"]} == {"search_knowledge", "read_document"}
    request = {"name": "search_knowledge", "arguments": {"query": "rotating", "k": 4}}
    results = (await _rpc(client, token, "tools/call", request)).json()["result"]["structuredContent"]
    assert [hit["citation"] for hit in results["results"]] == [f"{document}#chunk-1"]
    response = await _rpc(client, token, "tools/call", {"name": "read_document", "arguments": {"id": document}})
    assert response.json()["result"]["structuredContent"]["title"] == "Runbook"
    assert response.json()["result"]["structuredContent"]["text"] == "Procedure for rotating account keys"

    without_team = issue_knowledge_token(settings, _task(store, actor="guest"), "org", 60)
    for access_token, target in [(without_team, document), (token, foreign)]:
        request = {"name": "read_document", "arguments": {"id": target}}
        error = (await _rpc(client, access_token, "tools/call", request)).json()["result"]
        assert error["isError"] and "Runbook" not in repr(error)
    deleted = await client.delete(f"/v1/knowledge/documents/{document}",
                                  headers={"Authorization": "Bearer admin"})
    assert deleted.status_code == 204
    listed = await client.get("/v1/knowledge/documents", headers={"Authorization": "Bearer admin"})
    assert listed.json() == []


async def test_mcp_requires_task_token_and_current_grant(knowledge_api):
    client, store, settings = knowledge_api
    task_id = _task(store)
    token = issue_knowledge_token(settings, task_id, "org", 60)
    expired = issue_knowledge_token(settings, task_id, "org", -1)
    wrong_tenant = issue_knowledge_token(settings, task_id, "other", 60)
    unselected = issue_knowledge_token(settings, _task(store, tools=[]), "org", 60)
    for invalid in (expired, wrong_tenant, "admin", token + "tampered"):
        assert (await _rpc(client, invalid, "tools/list")).status_code == 401
    assert (await _rpc(client, unselected, "tools/list")).status_code == 403
    assert (await _rpc(client, token, "tools/list", headers={"Origin": "https://other.example.org"})).status_code == 403
    assert (await _rpc(client, token, "tools/list", headers={"MCP-Protocol-Version": "invalid"})).status_code == 400
    assert (await client.get(PATH, headers={"Authorization": f"Bearer {token}"})).status_code == 405
    bad = await _rpc(client, token, "tools/call", {"name": "search_knowledge", "arguments": {"query": "", "k": 0}})
    assert bad.json()["error"]["code"] == -32602
    store.db.grants.clear()
    assert (await _rpc(client, token, "tools/list")).status_code == 403
    store.db.grants.add("org")
    store.db.tasks["org", task_id].state = TaskState.SUCCEEDED
    assert (await _rpc(client, token, "tools/list")).status_code == 403


def test_keyword_terms_drop_stopwords_stem_and_split_cjk():
    from sca.db.repo import keyword_terms

    assert keyword_terms("How are discounts rounded?") == ["discount", "round"]
    assert keyword_terms("折扣规则") == ["折扣", "扣规", "规则"]
