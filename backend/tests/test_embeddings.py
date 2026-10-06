import os
import time

import fakeredis.aioredis
import pytest

from app.core.config import Settings
from app.db.models import MemoryRecord
from app.providers.embeddings import Embedder, encode_local
from app.schemas.research import ResearchRequest


async def test_embedding_cache_is_owner_scoped_and_bounded(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    calls = []

    def encode(texts, settings):
        calls.append(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]

    monkeypatch.setattr("app.providers.embeddings.encode_local", encode)
    first = Embedder(Settings(), redis, "a")
    await first.encode(["Private memory"])
    await first.encode(["Private memory"])
    assert len(calls) == 1
    await Embedder(Settings(), redis, "b").encode(["Private memory"])
    assert len(calls) == 2
    with pytest.raises(Exception, match="resource bound"):
        await first.encode(["x"] * 129)
    await redis.aclose()


async def test_semantic_memory_filters_owner_expiry_and_model(system):
    _, _, repo, _, owner, other = system
    one = await repo.create_job(owner, ResearchRequest(query="Owner's memory"), "vector-owner-001", {})
    two = await repo.create_job(other, ResearchRequest(query="Other's memory"), "vector-other-001", {})
    vector = [1.0] + [0.0] * 383
    async with repo.sessions.begin() as db:
        for user, job, content, model, expiry in [
            (owner, one, "Accessible", "m1", time.time() + 100),
            (other, two, "Private", "m1", time.time() + 100),
            (owner, one, "Expired", "m1", 1),
            (owner, one, "Wrong model", "m2", time.time() + 100),
        ]:
            db.add(
                MemoryRecord(
                    user_id=user,
                    job_id=job.job_id,
                    session_id=job.session_id,
                    content=content,
                    content_hash=content,
                    evidence=[],
                    expires_at=expiry,
                    embedding=vector,
                    embedding_model=model,
                )
            )
    rows = await repo.semantic_memory(owner, vector, "m1")
    assert [r["content"] for r in rows] == ["Accessible"]


@pytest.mark.requires_embedding_model
def test_real_local_model_paraphrase_ranking():
    if not os.getenv("RUN_EMBEDDING_TESTS"):
        pytest.skip("Set RUN_EMBEDDING_TESTS=1 after provisioning the pinned model")
    vectors = encode_local(
        [
            "How do plants turn sunlight into energy?",
            "Photosynthesis converts solar energy into chemical energy.",
            "The concert tickets sold out yesterday.",
        ],
        Settings(),
    )
    assert len(vectors[0]) == 384
    relevant = sum(a * b for a, b in zip(vectors[0], vectors[1]))
    unrelated = sum(a * b for a, b in zip(vectors[0], vectors[2]))
    assert relevant > unrelated
