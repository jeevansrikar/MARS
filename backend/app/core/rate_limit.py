import hashlib

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import AppError

SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


async def rate_limit(redis: Redis, identity: str, endpoint: str, limit: int, seconds: int = 60):
    key = hashlib.sha256(f"{identity}:{endpoint}".encode()).hexdigest()
    try:
        count = await redis.eval(SCRIPT, 1, f"limit:{key}", seconds)
    except RedisError as exc:
        raise AppError("DEPENDENCY_UNAVAILABLE", "Rate limiting unavailable; retry later", 503) from exc
    if int(count) > limit:
        raise AppError("RATE_LIMITED", "Too many requests; retry later", 429)
