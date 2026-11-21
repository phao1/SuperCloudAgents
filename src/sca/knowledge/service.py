import logging
from dataclasses import asdict
from uuid import UUID

from sca.config import Settings
from sca.core.errors import InvalidTaskError
from sca.core.interfaces import ModelGateway
from sca.db.repo import Store
from sca.knowledge.chunks import split_document
from sca.knowledge.models import Citation, DocumentInput, DocumentMetadata, DocumentRead, SearchHit
from sca.memory.embedding import embed, embedding_gateway, literal

logger = logging.getLogger(__name__)


async def _index_embeddings(texts: list[str], settings: Settings,
                            gateway: ModelGateway | None) -> list[list[float] | None]:
    vectors: list[list[float] | None] = [None] * len(texts)
    model = settings.memory_embedding_model
    if not model:
        return vectors
    async with embedding_gateway(settings, gateway) as client:
        if client is not None:
            for start in range(0, len(texts), 32):
                batch = texts[start:start + 32]
                try:
                    embedded = await client.embed(batch, model)
                    if len(embedded) != len(batch):
                        raise ValueError("Unexpected embedding count")
                    for vector in embedded:
                        literal(vector)
                    vectors[start:start + len(batch)] = [vector if any(vector) else None
                                                          for vector in embedded]
                except Exception:  # noqa: BLE001 - keyword indexing survives embedding outages
                    logger.warning("Knowledge embeddings unavailable")
    return vectors


async def ingest(store: Store, body: DocumentInput, actor: str, settings: Settings,
                 gateway: ModelGateway | None = None) -> DocumentMetadata:
    chunks = split_document(body.text)
    if not chunks:
        raise InvalidTaskError("知识库文档不能为空")
    vectors = await _index_embeddings(
        [f"{body.title}\n{chunk.heading or ''}\n{chunk.content}" for chunk in chunks], settings, gateway,
    )
    row = await store.create_knowledge_document(
        {"title": body.title, "content": body.text, "format": body.format,
         "allowed_teams": sorted(set(body.allowed_teams)), "created_by": actor,
         "project_id": body.project_id},
        [{**asdict(chunk), "embedding": vector} for chunk, vector in zip(chunks, vectors, strict=True)],
        settings.memory_embedding_model,
    )
    return DocumentMetadata.model_validate(row)


def _citation(document_id: UUID, title: str, chunk: dict) -> Citation:
    ordinal = chunk["ordinal"]
    return Citation(document_id=document_id, title=title, ordinal=ordinal, heading=chunk["heading"],
                    citation=f"{document_id}#chunk-{ordinal}")


async def search(store: Store, teams: list[str], query: str, k: int, settings: Settings,
                 gateway: ModelGateway | None = None, *,
                 project_id: UUID | None = None) -> list[SearchHit]:
    vector = await embed(query, settings, gateway)
    rows = await store.search_knowledge(
        query, teams, k, vector, settings.memory_embedding_model, project_id=project_id,
    )
    return [SearchHit(
        **_citation(row["document_id"], row["title"], row).model_dump(), text=row["content"],
        score=1 - row["distance"] if row["distance"] is not None else None,
    ) for row in rows]


async def read_document(
    store: Store, teams: list[str], document_id: UUID, *, project_id: UUID | None = None,
) -> DocumentRead:
    row = await store.get_knowledge_document(document_id, teams, project_id=project_id)
    return DocumentRead(id=document_id, title=row["title"], format=row["format"], text=row["content"],
                        citations=[_citation(document_id, row["title"], c) for c in row["citations"]])
