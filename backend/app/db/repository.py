"""Short transactions; every worker write is protected by a database lease fence."""

import hashlib
import time
from typing import cast
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, JobCancelled, LeaseLost
from app.db.models import (
    AgentRun,
    AuditRecord,
    ClaimRecord,
    ClaimSource,
    Event,
    Job,
    Membership,
    MemoryRecord,
    Organization,
    ResultRecord,
    SourceRecord,
    User,
)
from app.schemas.research import TERMINAL, JobStatus, ResearchEvent, ResearchJob, ResearchRequest, ResearchResult


def event_view(event: Event) -> ResearchEvent:
    return ResearchEvent(
        event_id=str(event.sequence),
        sequence=event.sequence,
        job_id=event.job_id,
        type=event.type,
        agent=event.agent,
        timestamp=event.timestamp,
        payload=event.payload,
    )


def append_event(db: AsyncSession, job: Job, kind: str, payload: dict, agent: str | None = None):
    job.last_sequence += 1
    db.add(Event(job_id=job.id, sequence=job.last_sequence, type=kind, payload=payload, agent=agent))


def memory_access(owner: str, session_id: str | None, organization_id: str | None):
    if organization_id:
        memberships = select(Membership.organization_id).where(Membership.user_id == owner)
        return and_(
            MemoryRecord.scope == "organization",
            MemoryRecord.organization_id == organization_id,
            MemoryRecord.organization_id.in_(memberships),
        )
    scope = MemoryRecord.scope == "user"
    if session_id:
        scope = or_(scope, and_(MemoryRecord.scope == "session", MemoryRecord.session_id == session_id))
    return and_(MemoryRecord.user_id == owner, scope)


def memory_view(row: MemoryRecord, owner: str) -> dict:
    return {
        "id": row.id,
        "content": row.content,
        "job_id": row.job_id,
        "evidence": row.evidence,
        "expires_at": row.expires_at,
        "scope": row.scope,
        "organization_id": row.organization_id,
        "can_delete": row.user_id == owner,
    }


class Repository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings

    async def create_job(self, owner: str, request: ResearchRequest, key: str, trace_context: dict) -> ResearchJob:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self.sessions.begin() as db:
            # Serializes idempotency, daily quotas and active-job checks for this user.
            await db.scalar(select(User).where(User.id == owner).with_for_update())
            if request.organization_id and not await db.get(Membership, (str(request.organization_id), owner)):
                raise AppError("FORBIDDEN", "Organization membership required", 403)
            job = await db.scalar(select(Job).where(Job.user_id == owner, Job.idempotency_key == key))
            if job:
                if job.request_hash != fingerprint:
                    raise AppError("IDEMPOTENCY_CONFLICT", "Key already used for a different request", 409)
            else:
                active = await db.scalar(
                    select(func.count()).select_from(Job).where(Job.user_id == owner, Job.status.not_in(TERMINAL))
                )
                daily = await db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.user_id == owner, Job.created_at > time.time() - 86400)
                )
                if (active or 0) >= self.settings.max_active_jobs or (daily or 0) >= self.settings.daily_job_quota:
                    raise AppError("RATE_LIMITED", "Research quota reached", 429)
                if request.session_id:
                    exists = await db.scalar(
                        select(Job.id).where(Job.user_id == owner, Job.session_id == str(request.session_id)).limit(1)
                    )
                    if not exists:
                        raise AppError("SESSION_NOT_FOUND", "Session not found", 404)
                job = Job(
                    user_id=owner,
                    query=request.query,
                    memory_scope=request.memory_scope,
                    organization_id=str(request.organization_id) if request.organization_id else None,
                    session_id=str(request.session_id or uuid4()),
                    idempotency_key=key,
                    request_hash=fingerprint,
                    trace_context=trace_context,
                )
                db.add(job)
                await db.flush()
                append_event(db, job, "job_queued", {"status": "queued"})
                db.add(AuditRecord(user_id=owner, action="job_created", target_id=job.id))
            job_id = job.id
        return await self.get_job(owner, job_id)

    async def get_job(self, owner: str, job_id: str) -> ResearchJob:
        async with self.sessions() as db:
            job = await db.scalar(select(Job).where(Job.id == job_id, Job.user_id == owner))
            if not job:
                raise AppError("JOB_NOT_FOUND", "Research job not found", 404)
            result = await db.get(ResultRecord, job_id)
            stages = (await db.scalars(select(AgentRun).where(AgentRun.job_id == job_id))).all()
            return ResearchJob(
                job_id=job.id,
                session_id=job.session_id,
                query=job.query,
                memory_scope=job.memory_scope,
                organization_id=job.organization_id,
                status=cast(JobStatus, job.status),
                current_stage=job.current_stage,
                progress=job.progress,
                attempt=job.attempt,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                error_code=job.error_code,
                error_message=job.error_message,
                last_sequence=job.last_sequence,
                result=ResearchResult.model_validate(result.document) if result else None,
                stages={
                    r.agent: {
                        "status": r.status,
                        "output": r.output,
                        "error_code": r.error_code,
                        "latency_ms": r.latency_ms,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                    }
                    for r in stages
                },
            )

    async def list_jobs(self, owner: str, before: float, limit: int) -> list[dict]:
        async with self.sessions() as db:
            jobs = (
                await db.scalars(
                    select(Job)
                    .where(Job.user_id == owner, Job.created_at < before)
                    .order_by(Job.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [{"job_id": j.id, "query": j.query, "status": j.status, "created_at": j.created_at} for j in jobs]

    async def cancel(self, owner: str, job_id: str) -> ResearchJob:
        async with self.sessions.begin() as db:
            job = await db.scalar(select(Job).where(Job.id == job_id, Job.user_id == owner).with_for_update())
            if not job:
                raise AppError("JOB_NOT_FOUND", "Research job not found", 404)
            if job.status not in TERMINAL and job.status != "cancelling":
                job.status = "cancelling"
                append_event(db, job, "job_cancelling", {"status": job.status})
        return await self.get_job(owner, job_id)

    async def events(self, owner: str, job_id: str, after: int) -> list[ResearchEvent]:
        async with self.sessions() as db:
            owned = await db.scalar(select(Job.id).where(Job.id == job_id, Job.user_id == owner))
            if not owned:
                raise AppError("JOB_NOT_FOUND", "Research job not found", 404)
            rows = (
                await db.scalars(
                    select(Event)
                    .where(Event.job_id == job_id, Event.sequence > after)
                    .order_by(Event.sequence)
                    .limit(100)
                )
            ).all()
            return [event_view(row) for row in rows]

    async def claim_job(self) -> Job | None:
        now = time.time()
        async with self.sessions.begin() as db:
            job = await db.scalar(
                select(Job)
                .where(Job.status.not_in(TERMINAL), Job.lease_until < now)
                .order_by(Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return None
            if job.status == "cancelling":
                await self._end_running(db, job, JobCancelled())
                self._terminal(db, job, "cancelled")
                return None
            if job.attempt >= self.settings.max_attempts or (
                job.started_at and now - job.started_at > self.settings.max_job_seconds
            ):
                job.error_code = "RETRY_EXHAUSTED"
                job.error_message = "Research could not finish within its execution budget"
                await self._end_running(db, job, AppError(job.error_code, job.error_message))
                self._terminal(db, job, "failed")
                return None
            job.status = "running"
            job.attempt += 1
            job.lease_token = str(uuid4())
            job.lease_until = now + self.settings.lease_seconds
            job.started_at = job.started_at or now
            job.error_code = job.error_message = None
            append_event(db, job, "job_started", {"status": job.status, "attempt": job.attempt})
            await db.flush()
            return job

    async def fenced(self, db: AsyncSession, job_id: str, token: str, allow_cancel: bool = False) -> Job:
        job = await db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.lease_token != token or job.lease_until < time.time() or job.status in TERMINAL:
            raise LeaseLost()
        if job.status == "cancelling" and not allow_cancel:
            raise JobCancelled()
        return job

    async def heartbeat(self, job_id: str, token: str):
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token)
            job.lease_until = time.time() + self.settings.lease_seconds

    async def reserve_call(self, job_id: str, token: str, kind: str):
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token)
            field, maximum = (
                ("search_calls", self.settings.max_search_calls)
                if kind == "search"
                else ("agent_calls", self.settings.max_agent_calls)
            )
            if getattr(job, field) >= maximum:
                raise AppError("BUDGET_EXHAUSTED", "Research reached its provider call budget")
            setattr(job, field, getattr(job, field) + 1)

    async def stage_start(self, job_id: str, token: str, stage: str, progress: float) -> dict | None:
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token)
            run = await db.scalar(select(AgentRun).where(AgentRun.job_id == job_id, AgentRun.agent == stage))
            if run and run.status == "completed":
                return run.output
            if not run:
                run = AgentRun(job_id=job_id, agent=stage, status="running", attempt=1)
                db.add(run)
            else:
                run.status, run.started_at, run.attempt = "running", time.time(), run.attempt + 1
                run.error_code = None
            job.current_stage, job.progress = stage, max(progress, job.progress)
            append_event(db, job, "agent_started", {"status": "running", "progress": job.progress}, stage)
            return None

    async def stage_complete(self, job_id: str, token: str, stage: str, output: dict):
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token)
            run = await db.scalar(select(AgentRun).where(AgentRun.job_id == job_id, AgentRun.agent == stage))
            if not run or run.status != "running":
                raise LeaseLost()
            run.output, run.status, run.completed_at = output, "completed", time.time()
            run.latency_ms = (run.completed_at - run.started_at) * 1000
            run.model = output.get("model")
            run.input_tokens = output.get("input_tokens", 0)
            run.output_tokens = output.get("output_tokens", 0)
            # Source text stays in checkpoint storage, not in every SSE frame.
            public = {k: v for k, v in output.items() if k not in {"sources", "memory", "embeddings"}}
            append_event(db, job, "agent_completed", public, stage)

    def _terminal(self, db: AsyncSession, job: Job, status: str):
        job.status, job.completed_at, job.lease_until = status, time.time(), 0
        job.lease_token = None
        if status == "cancelled":
            job.cancelled_at = job.completed_at
        if status.startswith("completed"):
            job.progress = 1
        append_event(
            db,
            job,
            "job_completed" if status.startswith("completed") else f"job_{status}",
            {"status": status, "error_code": job.error_code, "error_message": job.error_message},
        )

    async def finish(
        self,
        job_id: str,
        token: str,
        result: ResearchResult,
        embeddings: dict | None = None,
        embedding_model: str | None = None,
    ):
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token)
            if job.memory_scope == "organization":
                membership = await db.scalar(
                    select(Membership)
                    .where(Membership.organization_id == job.organization_id, Membership.user_id == job.user_id)
                    .with_for_update()
                )
                if not membership:
                    raise AppError("FORBIDDEN", "Organization membership was revoked", 403)
            for source in result.sources:
                db.add(SourceRecord(job_id=job_id, **source.model_dump()))
            await db.flush()
            for claim in result.claims:
                db.add(
                    ClaimRecord(
                        id=claim.id, job_id=job_id, claim=claim.claim, status=claim.status, reasoning=claim.reasoning
                    )
                )
            await db.flush()
            for claim in result.claims:
                for evidence in {e.source_id: e for e in claim.evidence}.values():
                    db.add(
                        ClaimSource(
                            claim_id=claim.id,
                            source_id=evidence.source_id,
                            support_type=claim.status,
                            evidence_text=evidence.passage,
                        )
                    )
                if claim.status == "supported":
                    db.add(
                        MemoryRecord(
                            user_id=job.user_id,
                            job_id=job_id,
                            session_id=job.session_id,
                            scope=job.memory_scope,
                            organization_id=job.organization_id,
                            content=claim.claim,
                            content_hash=hashlib.sha256(claim.claim.encode()).hexdigest(),
                            evidence=[e.model_dump() for e in claim.evidence],
                            embedding=(embeddings or {}).get(claim.id),
                            embedding_model=embedding_model if claim.id in (embeddings or {}) else None,
                            expires_at=time.time() + self.settings.memory_ttl_days * 86400,
                        )
                    )
            db.add(
                ResultRecord(
                    job_id=job_id,
                    summary=result.summary,
                    answer=result.answer,
                    citations=[c.model_dump() for c in result.citations],
                    quality_metrics=result.quality_metrics.model_dump(),
                    document=result.model_dump(),
                )
            )
            self._terminal(db, job, "completed_with_warnings" if result.warnings else "completed")

    async def fail(self, job_id: str, token: str, error: AppError):
        async with self.sessions.begin() as db:
            job = await self.fenced(db, job_id, token, allow_cancel=True)
            await self._end_running(db, job, error)
            if job.status == "cancelling" or isinstance(error, JobCancelled):
                self._terminal(db, job, "cancelled")
                return
            job.error_code, job.error_message = error.code, error.message
            if error.retryable and job.attempt < self.settings.max_attempts:
                job.status, job.lease_token = "queued", None
                job.lease_until = time.time() + min(30, 2**job.attempt)
                append_event(db, job, "job_retrying", {"status": "queued", "error_code": error.code})
            else:
                self._terminal(db, job, "failed")

    async def _end_running(self, db: AsyncSession, job: Job, error: AppError):
        cancelled = job.status == "cancelling" or isinstance(error, JobCancelled)
        runs = (await db.scalars(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.status == "running"))).all()
        for run in runs:
            run.status = "cancelled" if cancelled else "failed"
            run.completed_at, run.error_code = time.time(), error.code
            run.latency_ms = (run.completed_at - run.started_at) * 1000
            append_event(db, job, f"agent_{run.status}", {"error_code": error.code, "notes": error.message}, run.agent)

    async def memory(
        self, owner: str, session_id: str | None = None, limit: int = 100, organization_id: str | None = None
    ) -> list[dict]:
        async with self.sessions() as db:
            rows = (
                await db.scalars(
                    select(MemoryRecord)
                    .where(MemoryRecord.expires_at > time.time(), memory_access(owner, session_id, organization_id))
                    .order_by(MemoryRecord.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [memory_view(r, owner) for r in rows]

    async def delete_memory(self, owner: str, memory_id: str):
        async with self.sessions.begin() as db:
            await db.execute(delete(MemoryRecord).where(MemoryRecord.user_id == owner, MemoryRecord.id == memory_id))
            db.add(AuditRecord(user_id=owner, action="memory_deleted", target_id=memory_id))

    async def semantic_memory(
        self,
        owner: str,
        vector: list[float],
        model: str,
        session_id: str | None = None,
        limit: int = 10,
        organization_id: str | None = None,
    ) -> list[dict]:
        async with self.sessions() as db:
            query = select(MemoryRecord).where(
                MemoryRecord.expires_at > time.time(),
                memory_access(owner, session_id, organization_id),
                MemoryRecord.embedding_model == model,
                MemoryRecord.embedding.is_not(None),
            )
            if self.settings.database_url.startswith("postgresql"):
                query = query.order_by(MemoryRecord.embedding.cosine_distance(vector)).limit(min(limit * 3, 30))
                rows = (await db.scalars(query)).all()
            else:
                # Small SQLite test fixtures; production filtering/ranking is in PostgreSQL.
                rows = sorted(
                    (await db.scalars(query)).all(),
                    key=lambda row: (
                        -sum(a * b for a, b in zip(row.embedding if row.embedding is not None else [], vector))
                    ),
                )[: limit * 3]
            unique: dict[str, dict] = {}
            for row in rows:
                unique.setdefault(row.content_hash, memory_view(row, owner))
            return list(unique.values())[:limit]

    async def organizations(self, owner: str) -> list[dict]:
        async with self.sessions() as db:
            rows = (await db.scalars(select(Organization).join(Membership).where(Membership.user_id == owner))).all()
            return [{"id": row.id, "name": row.name} for row in rows]
