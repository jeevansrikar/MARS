import asyncio
import time

import structlog
from redis.exceptions import RedisError
from sqlalchemy import select

from app.db.models import Event
from app.db.repository import event_view

log = structlog.get_logger()


async def notify_job(redis, job_id: str):
    try:
        await redis.lpush("mars:jobs", job_id)
        await redis.ltrim("mars:jobs", 0, 9999)
    except RedisError:
        log.warning("queue.notification_failed", job_id=job_id)


async def publish_events(repo, redis):
    """Transactional outbox. Stream IDs make retries idempotent."""
    async with repo.sessions.begin() as db:
        rows = (
            await db.scalars(
                select(Event)
                .where(Event.published.is_(False))
                .order_by(Event.job_id, Event.sequence)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        ).all()
        for event in rows:
            key = f"mars:events:{event.job_id}"
            script = """
local last = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
if #last == 0 or tonumber(string.match(last[1][1], '^(%d+)')) < tonumber(ARGV[1]) then
  redis.call('XADD', KEYS[1], 'MAXLEN', '~', 2000, ARGV[1] .. '-0', 'event', ARGV[2])
end
redis.call('EXPIRE', KEYS[1], 86400)
return 1
"""
            await redis.eval(script, 1, key, event.sequence, event_view(event).model_dump_json())
            event.published = True


async def event_stream(request, owner: str, job_id: str, after: int, connection_id: str):
    from app.api.auth import COOKIE, token_hash
    from app.core.telemetry import STREAMS
    from app.db.models import AuthSession
    from app.schemas.research import TERMINAL

    repo, redis = request.app.state.repo, request.app.state.redis
    key = f"mars:connections:{owner}"
    STREAMS.inc()
    try:
        while not await request.is_disconnected():
            token = request.cookies.get(COOKIE, "")
            if request.headers.get("authorization", "").startswith("Bearer "):
                token = request.headers["authorization"][7:]
            async with repo.sessions() as db:
                auth = await db.get(AuthSession, token_hash(token))
                if not auth or auth.expires_at <= time.time():
                    return
            events = await repo.events(owner, job_id, after)
            for event in events:
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                after = event.sequence
            snapshot = await repo.get_job(owner, job_id)
            if snapshot.status in TERMINAL and after >= snapshot.last_sequence:
                return
            if events:
                continue
            try:
                await redis.zadd(key, {connection_id: time.time() + 30})
                await redis.expire(key, 60)
                await redis.xread({f"mars:events:{job_id}": f"{after}-0"}, count=1, block=10000)
            except RedisError:
                await asyncio.sleep(1)
            yield ": heartbeat\n\n"
    finally:
        STREAMS.dec()
        try:
            await redis.zrem(key, connection_id)
        except RedisError:
            pass
