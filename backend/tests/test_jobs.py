import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.errors import AppError, JobCancelled, LeaseLost
from app.db.models import Job
from app.schemas.research import ResearchRequest
from app.workers.queue import event_stream, publish_events


async def create(client, key="request-001", query="What evidence supports this claim?"):
    return await client.post("/api/v1/research", headers={"Idempotency-Key": key}, json={"query": query})


async def test_create_snapshot_idempotency_and_ownership(system):
    _, client, repo, _, owner, other = system
    response = await create(client)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert (await create(client)).json()["job_id"] == job["job_id"]
    assert (await create(client, query="A different query")).status_code == 409
    for suffix in ("", "/events"):
        assert (
            await client.get(f"/api/v1/research/{job['job_id']}{suffix}", headers={"Authorization": "Bearer token-b"})
        ).status_code == 404
    assert (
        await client.post(f"/api/v1/research/{job['job_id']}/cancel", headers={"Authorization": "Bearer token-b"})
    ).status_code == 404
    with pytest.raises(AppError):
        await repo.get_job(other, job["job_id"])
    assert len(await repo.list_jobs(owner, 1e20, 20)) == 1


async def test_validation_auth_csrf_and_size_limits(system):
    _, client, _, _, _, _ = system
    assert (await client.post("/api/v1/research", json={"query": "x"})).status_code == 422
    assert (await client.get("/api/v1/research", headers={"Authorization": ""})).status_code == 401
    assert (
        await client.post("/api/v1/research", headers={"Origin": "https://evil.invalid"}, json={})
    ).status_code == 403
    assert (await client.post("/api/v1/research", content="x" * 20000)).status_code == 413
    response = await client.post(
        "/api/v1/research",
        headers={"Idempotency-Key": "bad-owner-01"},
        json={"query": "test query", "user_id": "other"},
    )
    assert response.status_code == 422


async def test_checkpoints_lease_fence_and_crash_recovery(system):
    _, client, repo, _, owner, _ = system
    job_id = (await create(client)).json()["job_id"]
    first = await repo.claim_job()
    assert first.id == job_id
    assert await repo.claim_job() is None
    await repo.stage_start(job_id, first.lease_token, "planner", 0.1)
    await repo.stage_complete(job_id, first.lease_token, "planner", {"notes": "durable plan"})
    async with repo.sessions.begin() as db:
        row = await db.get(Job, job_id)
        row.lease_until = time.time() - 1
    second = await repo.claim_job()
    assert second.attempt == 2
    assert await repo.stage_start(job_id, second.lease_token, "planner", 0.1) == {"notes": "durable plan"}
    with pytest.raises(LeaseLost):
        await repo.stage_complete(job_id, first.lease_token, "planner", {"notes": "stale overwrite"})
    assert (await repo.get_job(owner, job_id)).stages["planner"]["output"]["notes"] == "durable plan"


async def test_queued_and_running_cancellation(system):
    _, client, repo, _, owner, _ = system
    one = (await create(client)).json()["job_id"]
    await repo.cancel(owner, one)
    assert await repo.claim_job() is None
    assert (await repo.get_job(owner, one)).status == "cancelled"
    two = (await create(client, "request-002")).json()["job_id"]
    claimed = await repo.claim_job()
    await repo.cancel(owner, two)
    with pytest.raises(JobCancelled):
        await repo.heartbeat(two, claimed.lease_token)
    await repo.fail(two, claimed.lease_token, JobCancelled())
    assert (await repo.get_job(owner, two)).status == "cancelled"


async def test_retry_and_exhaustion(system):
    _, client, repo, _, owner, _ = system
    job_id = (await create(client)).json()["job_id"]
    for attempt in range(repo.settings.max_attempts):
        async with repo.sessions.begin() as db:
            row = await db.get(Job, job_id)
            row.lease_until = 0
        job = await repo.claim_job()
        await repo.fail(job_id, job.lease_token, AppError("PROVIDER_TIMEOUT", "Timeout", 503, True))
        status = (await repo.get_job(owner, job_id)).status
        assert status == ("failed" if attempt == repo.settings.max_attempts - 1 else "queued")


async def test_event_replay_and_redis_loss(system):
    _, client, repo, redis, owner, _ = system
    job_id = (await create(client)).json()["job_id"]
    await repo.cancel(owner, job_id)
    await repo.claim_job()
    await publish_events(repo, redis)
    first = await repo.events(owner, job_id, 0)
    assert [e.sequence for e in first] == [1, 2, 3]
    await redis.delete(f"mars:events:{job_id}")
    response = await client.get(f"/api/v1/research/{job_id}/events", headers={"Last-Event-ID": "1"})
    assert "id: 1\n" not in response.text and "id: 2\n" in response.text and "id: 3\n" in response.text
    assert (await client.get(f"/api/v1/research/{job_id}/events", headers={"Last-Event-ID": "999"})).status_code == 400


async def test_browser_disconnect_does_not_cancel_worker(system):
    app, client, repo, _, owner, _ = system
    job_id = (await create(client)).json()["job_id"]
    job = await repo.claim_job()
    request = SimpleNamespace(
        app=app, cookies={}, headers={"authorization": "Bearer token-a"}, is_disconnected=asyncio_coroutine_false
    )
    stream = event_stream(request, owner, job_id, 0, "disconnect-test")
    assert "job_queued" in await anext(stream)
    await stream.aclose()
    await repo.stage_start(job_id, job.lease_token, "planner", 0.1)
    await repo.stage_complete(job_id, job.lease_token, "planner", {"notes": "completed after disconnect"})
    assert (await repo.get_job(owner, job_id)).status == "running"
    replay = await repo.events(owner, job_id, 1)
    assert any(e.type == "agent_completed" for e in replay)


async def asyncio_coroutine_false():
    return False


async def test_active_quota_and_resource_call_budget(system):
    _, client, repo, _, _, _ = system
    for i in range(3):
        assert (await create(client, f"request-{i:03}")).status_code == 202
    assert (await create(client, "request-004")).status_code == 429
    job = await repo.claim_job()
    for _ in range(repo.settings.max_search_calls):
        await repo.reserve_call(job.id, job.lease_token, "search")
    with pytest.raises(AppError, match="budget"):
        await repo.reserve_call(job.id, job.lease_token, "search")


async def test_ready_reports_missing_configuration_and_operator_auth(system):
    app, client, _, _, _, _ = system
    assert (await client.get("/live")).status_code == 200
    assert (await client.get("/ready")).status_code == 200
    app.state.settings.search_api_key = ""
    # Test configuration normally uses SecretStr; preserve its declared contract.
    from pydantic import SecretStr

    app.state.settings.search_api_key = SecretStr("")
    assert (await client.get("/ready")).status_code == 503
    assert (await client.get("/health")).status_code == 401
    metrics = await client.get("/metrics", headers={"Authorization": "Bearer test-operator-token"})
    assert metrics.status_code == 200 and "mars_http_requests" in metrics.text


@pytest.mark.integration
async def test_postgres_concurrent_idempotency_and_worker_claim(system):
    _, _, repo, _, owner, _ = system
    if not repo.settings.database_url.startswith("postgresql"):
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL locking integration")
    jobs = await asyncio.gather(
        *(repo.create_job(owner, ResearchRequest(query="Concurrent research"), "concurrent-001", {}) for _ in range(8))
    )
    assert len({j.job_id for j in jobs}) == 1
    claims = await asyncio.gather(*(repo.claim_job() for _ in range(8)))
    assert sum(j is not None for j in claims) == 1
