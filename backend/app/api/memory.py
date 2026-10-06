from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import current_user
from app.providers.embeddings import Embedder
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import RankedList, ReciprocalRankFusion

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.get("")
async def memories(
    request: Request,
    owner: str = Depends(current_user),
    query: str = Query(default="", max_length=2000),
    session_id: UUID | None = None,
    organization_id: UUID | None = None,
):
    session = str(session_id) if session_id else None
    organization = str(organization_id) if organization_id else None
    rows = await request.app.state.repo.memory(owner, session, organization_id=organization)
    if not query:
        return rows[:20]
    index = BM25Index()
    index.add_documents([(r["id"], r["content"]) for r in rows])
    by_id = {r["id"]: r for r in rows}
    rankers = [RankedList("bm25", [r.doc_id for r in index.search(query, top_k=10)])]
    if request.app.state.settings.semantic_enabled:
        embedder = Embedder(request.app.state.settings, request.app.state.redis, owner)
        vector = (await embedder.encode([query]))[0]
        semantic = await request.app.state.repo.semantic_memory(
            owner, vector, embedder.model_id, session, organization_id=organization
        )
        by_id.update({row["id"]: row for row in semantic})
        rankers.append(RankedList("dense", [row["id"] for row in semantic]))
    return [{**by_id[row.doc_id], "relevance": row.rrf_score} for row in ReciprocalRankFusion().fuse(rankers)[:10]]


@router.delete("/{memory_id}", status_code=204)
async def forget(memory_id: str, request: Request, owner: str = Depends(current_user)):
    await request.app.state.repo.delete_memory(owner, memory_id)
