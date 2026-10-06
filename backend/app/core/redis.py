import time

from opentelemetry.trace import Status, StatusCode
from redis.asyncio import Redis

from app.core.telemetry import DEPENDENCY_LATENCY, tracer


class InstrumentedRedis(Redis):
    async def execute_command(self, *args, **kwargs):
        command = str(args[0]).upper().split()[0] if args else "UNKNOWN"
        start = time.monotonic()
        with tracer.start_as_current_span(
            "redis.command",
            attributes={"db.system": "redis", "db.operation.name": command},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return await super().execute_command(*args, **kwargs)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            finally:
                DEPENDENCY_LATENCY.labels("redis", command).observe(time.monotonic() - start)


def create_redis(settings):
    return InstrumentedRedis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=15,
        max_connections=settings.redis_max_connections,
    )
