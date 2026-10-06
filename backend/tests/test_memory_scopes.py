import time

from sqlalchemy import delete

from app.db.models import Membership, MemoryRecord, Organization
from app.schemas.research import ResearchRequest


async def test_private_session_and_organization_memory_are_separate(system):
    _, client, repo, _, owner, other = system
    async with repo.sessions.begin() as db:
        org = Organization(name="Team A")
        db.add(org)
        await db.flush()
        db.add(Membership(organization_id=org.id, user_id=owner))
    job = await repo.create_job(owner, ResearchRequest(query="Scope isolation"), "scopes-001", {})
    vector = [1.0] + [0.0] * 383
    async with repo.sessions.begin() as db:
        for scope, text in [("user", "Private"), ("session", "Session only"), ("organization", "Shared")]:
            db.add(
                MemoryRecord(
                    user_id=owner,
                    job_id=job.job_id,
                    session_id=job.session_id,
                    scope=scope,
                    organization_id=org.id if scope == "organization" else None,
                    content=text,
                    content_hash=text,
                    evidence=[],
                    embedding=vector,
                    embedding_model="test",
                    expires_at=time.time() + 1000,
                )
            )
    assert [r["content"] for r in await repo.memory(owner)] == ["Private"]
    assert {r["content"] for r in await repo.memory(owner, job.session_id)} == {"Private", "Session only"}
    assert [r["content"] for r in await repo.memory(owner, organization_id=org.id)] == ["Shared"]
    assert await repo.memory(other, organization_id=org.id) == []
    denied = await client.post(
        "/api/v1/research",
        headers={"Authorization": "Bearer token-b", "Idempotency-Key": "forbidden-org-001"},
        json={"query": "Unauthorized sharing", "memory_scope": "organization", "organization_id": org.id},
    )
    assert denied.status_code == 403
    async with repo.sessions.begin() as db:
        db.add(Membership(organization_id=org.id, user_id=other))
    assert [r["content"] for r in await repo.semantic_memory(other, vector, "test", organization_id=org.id)] == [
        "Shared"
    ]
    shared = (await repo.memory(other, organization_id=org.id))[0]
    assert shared["can_delete"] is False
    await repo.delete_memory(other, shared["id"])
    assert len(await repo.memory(owner, organization_id=org.id)) == 1
    async with repo.sessions.begin() as db:
        await db.execute(delete(Membership).where(Membership.user_id == other))
    assert await repo.semantic_memory(other, vector, "test", organization_id=org.id) == []
    assert await repo.memory(other, organization_id=org.id) == []


async def test_shared_job_refuses_final_write_after_membership_revocation(system):
    from test_workflow import ControlledLLM, ControlledSearch

    from app.workers.run import execute_job

    _, _, repo, redis, owner, _ = system
    async with repo.sessions.begin() as db:
        org = Organization(name="Revocation test")
        db.add(org)
        await db.flush()
        db.add(Membership(organization_id=org.id, user_id=owner))
    job = await repo.create_job(
        owner,
        ResearchRequest(query="Samples experiment", memory_scope="organization", organization_id=org.id),
        "revocation-001",
        {},
    )
    claimed = await repo.claim_job()
    async with repo.sessions.begin() as db:
        await db.execute(delete(Membership).where(Membership.user_id == owner))
    await execute_job(repo, redis, claimed, llm_factory=ControlledLLM, search_factory=ControlledSearch)
    snapshot = await repo.get_job(owner, job.job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "FORBIDDEN"
    assert await repo.memory(owner, organization_id=org.id) == []
