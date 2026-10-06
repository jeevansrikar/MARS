import asyncio
import os
import signal
import socket
import time

import structlog
from opentelemetry.propagate import extract
from prometheus_client import start_http_server
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import AppError, LeaseLost
from app.core.redis import create_redis
from app.core.telemetry import JOB_DURATION, JOBS, QUEUE, WORKERS, safe_span, safe_stack, setup_telemetry
from app.db.models import Job
from app.db.repository import Repository
from app.db.session import make_database
from app.orchestration.workflow import Workflow
from app.providers.anthropic import AnthropicProvider
from app.providers.embeddings import Embedder, encode_local
from app.providers.search import SearchProvider
from app.workers.queue import publish_events

log = structlog.get_logger()


def unwrap(error):
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


async def execute_job(
    repo, redis, job, workflow_factory=Workflow, llm_factory=AnthropicProvider, search_factory=SearchProvider
):
    llm = llm_factory(repo.settings, lambda: repo.reserve_call(job.id, job.lease_token, "llm"))
    search = search_factory(
        repo.settings, redis, job.user_id, job.id, lambda: repo.reserve_call(job.id, job.lease_token, "search")
    )
    WORKERS.inc()
    task = None
    try:
        with safe_span(
            "research.job", context=extract(job.trace_context), attributes={"job.id": job.id, "user.id": job.user_id}
        ):
            embedder = Embedder(repo.settings, redis, job.user_id) if repo.settings.semantic_enabled else None
            task = asyncio.create_task(workflow_factory(repo, job, llm, search, embedder).run())
            deadline = job.started_at + repo.settings.max_job_seconds
            while not task.done():
                await asyncio.wait({task}, timeout=1)
                if not task.done():
                    await repo.heartbeat(job.id, job.lease_token)
                if time.time() > deadline:
                    raise AppError("JOB_TIMEOUT", "Research exceeded its time budget")
            await task
            JOBS.labels("completed").inc()
    except asyncio.CancelledError:
        # Shutdown does not turn into a user cancellation. Another worker resumes after lease expiry.
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    except BaseExceptionGroup as group:
        error = unwrap(group)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await record_failure(repo, job, error)
    except Exception as error:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await record_failure(repo, job, error)
    finally:
        WORKERS.dec()
        JOB_DURATION.observe(time.time() - job.started_at)
        await llm.close()
        await search.close()


async def record_failure(repo, job, error):
    if isinstance(error, LeaseLost):
        return
    safe = (
        error
        if isinstance(error, AppError)
        else AppError("RESEARCH_FAILED", "Research could not be completed", 503, True)
    )
    log.error(
        "job.failed",
        job_id=job.id,
        user_id=job.user_id,
        error_code=safe.code,
        error_type=type(error).__name__,
        stack=safe_stack(error),
    )
    JOBS.labels(safe.code).inc()
    try:
        await repo.fail(job.id, job.lease_token, safe)
    except LeaseLost:
        return


async def run_worker():
    cfg = get_settings()
    setup_telemetry(cfg, "mars-worker")
    if cfg.semantic_enabled:
        await asyncio.to_thread(encode_local, ["Worker embedding readiness"], cfg)
    engine, sessions = make_database(cfg)
    repo = Repository(sessions, cfg)
    redis = create_redis(cfg)
    start_http_server(int(os.getenv("WORKER_METRICS_PORT", "9101")))

    async def consume():
        while True:
            try:
                job = await repo.claim_job()
                if job:
                    await execute_job(repo, redis, job)
                else:
                    try:
                        await redis.blpop("mars:jobs", timeout=2)
                    except RedisError:
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("worker.loop_failed", error_type=type(exc).__name__)
                await asyncio.sleep(2)

    async def outbox():
        last_metrics = 0.0
        while True:
            try:
                await publish_events(repo, redis)
                await redis.set(f"mars:worker:{socket.gethostname()}:{os.getpid()}", str(time.time()), ex=15)
                if time.monotonic() - last_metrics >= 15:
                    async with sessions() as db:
                        counts = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())
                    for status in (
                        "queued",
                        "running",
                        "cancelling",
                        "cancelled",
                        "completed",
                        "completed_with_warnings",
                        "failed",
                    ):
                        QUEUE.labels(status).set(counts.get(status, 0))
                    last_metrics = time.monotonic()
            except Exception as exc:
                log.warning("worker.outbox_unavailable", error_type=type(exc).__name__)
            await asyncio.sleep(1)

    tasks = [asyncio.create_task(consume()) for _ in range(cfg.worker_concurrency)] + [asyncio.create_task(outbox())]
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass  # Windows asyncio runner handles Ctrl+C.
    try:
        await stopped.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_worker())
