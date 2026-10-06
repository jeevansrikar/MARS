import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from opentelemetry.propagate import inject

from app.api.auth import current_user
from app.core.errors import AppError
from app.schemas.research import ResearchJob, ResearchRequest
from app.workers.queue import event_stream, notify_job

router = APIRouter(prefix="/api/v1/research", tags=["research"])


@router.post("", status_code=202, response_model=ResearchJob)
async def submit(
    body: ResearchRequest,
    request: Request,
    owner: str = Depends(current_user),
    idempotency_key: str = Header(min_length=8, max_length=128),
):
    carrier: dict[str, str] = {}
    inject(carrier)
    carrier["request_id"] = request.state.request_id
    job = await request.app.state.repo.create_job(owner, body, idempotency_key, carrier)
    await notify_job(request.app.state.redis, job.job_id)
    return job


@router.get("")
async def history(
    request: Request,
    owner: str = Depends(current_user),
    before: float = Query(default=1e20),
    limit: int = Query(20, ge=1, le=100),
):
    return await request.app.state.repo.list_jobs(owner, before, limit)


@router.get("/{job_id}", response_model=ResearchJob)
async def status(job_id: UUID, request: Request, owner: str = Depends(current_user)):
    return await request.app.state.repo.get_job(owner, str(job_id))


@router.post("/{job_id}/cancel", response_model=ResearchJob)
async def cancel(job_id: UUID, request: Request, owner: str = Depends(current_user)):
    job = await request.app.state.repo.cancel(owner, str(job_id))
    await notify_job(request.app.state.redis, job.job_id)
    return job


@router.get("/{job_id}/events")
async def events(
    job_id: UUID,
    request: Request,
    owner: str = Depends(current_user),
    last_event_id: str = Header(default="0"),
    after: int = Query(0, ge=0),
):
    job = await request.app.state.repo.get_job(owner, str(job_id))
    try:
        cursor = max(after, int(last_event_id))
        if cursor < 0 or cursor > job.last_sequence:
            raise ValueError()
    except ValueError as exc:
        raise AppError("INVALID_CURSOR", "Invalid event cursor", 400) from exc
    nonce = str(uuid4())
    script = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= 4 then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], 60)
return 1
"""
    admitted = await request.app.state.redis.eval(
        script, 1, f"mars:connections:{owner}", time.time(), time.time() + 30, nonce
    )
    if not admitted:
        raise AppError("RATE_LIMITED", "Too many open research streams", 429)
    return StreamingResponse(
        event_stream(request, owner, str(job_id), cursor, nonce),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )
