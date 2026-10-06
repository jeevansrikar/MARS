import asyncio
import json
import time

import pytest

from app.providers.base import Generation
from app.schemas.research import ResearchRequest, Source
from app.workers.run import execute_job


class ControlledLLM:
    def __init__(self, settings, reserve):
        self.reserve = reserve

    async def generate(self, system, content, model, max_tokens):
        await self.reserve()
        data = json.loads(content)
        if "Decompose" in system:
            output = {"queries": ["experiment samples"], "notes": "Check primary evidence"}
        elif "Rank supplied" in system:
            output = {"indices": [0]}
        elif "Researcher" in system:
            output = {
                "findings": [
                    {
                        "id": "temporary",
                        "claim": "The experiment measured 42 samples.",
                        "evidence": [
                            {"source_id": data["passages"][0]["source_id"], "passage": "experiment measured 42 samples"}
                        ],
                    }
                ]
            }
        elif "Critic" in system:
            output = {"conflicts": [], "notes": "No conflicting source found"}
        elif "Skeptic" in system:
            output = {"notes": "Only one primary report; no independent replication"}
        elif "Synthesizer" in system:
            output = {"claim_ids": [data["claims"][0]["id"]]}
        elif "final verifier" in system:
            output = {
                "verdicts": [
                    {"id": c["id"], "status": "supported", "reasoning": "Exact count in retrieved passage"}
                    for c in data["claims"]
                ]
            }
        else:
            raise AssertionError(system)
        return Generation(text=json.dumps(output), model=model, input_tokens=10, output_tokens=20)

    async def close(self):
        return None


class ControlledSearch:
    def __init__(self, settings, redis, owner, job_id, reserve):
        self.job_id, self.reserve = job_id, reserve

    async def search(self, query):
        await self.reserve()
        return [{"url": "https://example.org/experiment", "title": "Primary report"}]

    async def fetch_url(self, url, title):
        return Source(
            id=f"{self.job_id}:source1",
            url=url,
            canonical_url=url,
            title=title,
            publisher="example.org",
            content="The experiment measured 42 samples and documented the methods in detail.",
            content_hash="test-content-hash",
            retrieved_at=time.time(),
        )

    async def close(self):
        return None


async def test_api_worker_result_memory_and_replay(system):
    _, client, repo, redis, owner, other = system
    response = await client.post(
        "/api/v1/research",
        headers={"Idempotency-Key": "full-workflow-001"},
        json={"query": "How many samples were measured?"},
    )
    job_id = response.json()["job_id"]
    claimed = await repo.claim_job()
    await execute_job(repo, redis, claimed, llm_factory=ControlledLLM, search_factory=ControlledSearch)
    result = (await client.get(f"/api/v1/research/{job_id}")).json()
    assert result["status"] == "completed_with_warnings"
    assert "42 samples" in result["result"]["answer"]
    assert result["result"]["quality_metrics"]["citation_coverage"] == 1
    assert not any(k.startswith("debate_") for k in result["stages"])
    assert len(await repo.memory(owner)) == 1
    assert await repo.memory(other) == []
    memory = (await repo.memory(owner))[0]
    await repo.delete_memory(other, memory["id"])
    assert len(await repo.memory(owner)) == 1
    await repo.delete_memory(owner, memory["id"])
    assert await repo.memory(owner) == []
    stream = await client.get(f"/api/v1/research/{job_id}/events")
    assert "event: job_completed" in stream.text
    assert "agent_completed" in stream.text


async def test_active_provider_call_is_cancelled(system):
    _, _, repo, redis, owner, _ = system
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class SlowLLM(ControlledLLM):
        async def generate(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    job = await repo.create_job(owner, ResearchRequest(query="Slow research test"), "cancel-active-001", {})
    claimed = await repo.claim_job()
    worker = asyncio.create_task(
        execute_job(repo, redis, claimed, llm_factory=SlowLLM, search_factory=ControlledSearch)
    )
    await asyncio.wait_for(entered.wait(), 3)
    await repo.cancel(owner, job.job_id)
    await asyncio.wait_for(worker, 5)
    assert cancelled.is_set()
    assert (await repo.get_job(owner, job.job_id)).status == "cancelled"


async def test_no_search_evidence_never_produces_a_fabricated_answer(system):
    _, _, repo, redis, owner, _ = system

    class EmptySearch(ControlledSearch):
        async def search(self, query):
            return []

    job = await repo.create_job(owner, ResearchRequest(query="Unavailable evidence test"), "no-evidence-001", {})
    await execute_job(repo, redis, await repo.claim_job(), llm_factory=ControlledLLM, search_factory=EmptySearch)
    snapshot = await repo.get_job(owner, job.job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "NO_EVIDENCE" and snapshot.result is None


@pytest.mark.requires_embedding_model
async def test_real_embeddings_integrate_with_workflow_and_durable_memory(system):
    import os

    if not os.getenv("RUN_EMBEDDING_TESTS"):
        pytest.skip("Set RUN_EMBEDDING_TESTS=1 after provisioning the pinned model")
    _, _, repo, redis, owner, other = system
    repo.settings.semantic_enabled = True
    job = await repo.create_job(
        owner, ResearchRequest(query="How many samples were measured?"), "embedded-workflow-001", {}
    )
    await execute_job(repo, redis, await repo.claim_job(), llm_factory=ControlledLLM, search_factory=ControlledSearch)
    snapshot = await repo.get_job(owner, job.job_id)
    assert snapshot.status == "completed_with_warnings"
    assert "dense" in snapshot.stages["retrieval"]["output"]["rankers"]
    from app.providers.embeddings import Embedder

    embedder = Embedder(repo.settings, redis, owner)
    vector = (await embedder.encode(["Number of specimens in the experiment"]))[0]
    assert len(await repo.semantic_memory(owner, vector, embedder.model_id)) == 1
    assert await repo.semantic_memory(other, vector, embedder.model_id) == []
    assert snapshot.stages["beliefs"]["output"]["belief_graph"]["nodes"][0]["status"] == "supported"
