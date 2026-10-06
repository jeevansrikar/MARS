import asyncio
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from opentelemetry.propagate import extract
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api import auth, memory, research
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.redis import create_redis
from app.core.telemetry import LATENCY, REQUESTS, safe_span, setup_telemetry
from app.db.repository import Repository
from app.db.session import make_database

log = structlog.get_logger()


class RequestGuard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = headers.get(b"origin", b"").decode()
            if headers.get(b"x-mars-client") != b"1" or (origin and origin not in self.settings.allowed_origins):
                return await JSONResponse(
                    {"code": "FORBIDDEN", "message": "Invalid request origin or client header"}, 403
                )(scope, receive, send)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 16384:
                    return await JSONResponse({"code": "INVALID_REQUEST", "message": "Request body too large"}, 413)(
                        scope, receive, send
                    )
                if not message.get("more_body"):
                    break
            consumed = False

            async def limited_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, limited_receive, send)
        else:
            await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, sessions=None, redis=None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        setup_telemetry(cfg, "mars-api")
        engine, factory = make_database(cfg) if sessions is None else (None, sessions)
        app.state.settings, app.state.sessions = cfg, factory
        app.state.repo = Repository(factory, cfg)
        app.state.redis = redis or create_redis(cfg)
        app.state.dummy_password_hash = await asyncio.to_thread(auth.hasher.hash, secrets.token_urlsafe(32))
        yield
        if redis is None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="MARS", version="2.0.0", lifespan=lifespan)
    app.add_middleware(RequestGuard, settings=cfg)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "Last-Event-ID", "X-MARS-Client"],
    )

    @app.middleware("http")
    async def observe(request, call_next):
        start, request_id = time.monotonic(), str(uuid4())
        request.state.request_id = request_id
        with safe_span(
            "http.request",
            context=extract(request.headers),
            attributes={"http.request.method": request.method, "request.id": request_id},
        ) as span:
            response = await call_next(request)
            route = getattr(request.scope.get("route"), "path", "unmatched")
            span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", response.status_code)
            REQUESTS.labels(request.method, route, response.status_code).inc()
            LATENCY.labels(route).observe(time.monotonic() - start)
            response.headers.update(
                {
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store",
                }
            )
            log.info(
                "http.request",
                request_id=request_id,
                route=route,
                status=response.status_code,
                user_id=getattr(request.state, "user_id", None),
                latency_ms=round((time.monotonic() - start) * 1000),
            )
            return response

    @app.exception_handler(AppError)
    async def application_error(request, error):
        return JSONResponse(
            {"code": error.code, "message": error.message, "request_id": getattr(request.state, "request_id", None)},
            error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"code": "INVALID_REQUEST", "message": "Request fields are invalid"}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        log.error(
            "http.failure", error_type=type(error).__name__, request_id=getattr(request.state, "request_id", None)
        )
        return JSONResponse({"code": "INTERNAL_ERROR", "message": "Request could not be completed"}, 500)

    async def diagnostics():
        checks = {
            "postgres": False,
            "redis": False,
            "providers_configured": bool(
                cfg.anthropic_api_key.get_secret_value() and cfg.search_api_key.get_secret_value()
            ),
        }
        try:
            async with asyncio.timeout(4):
                async with app.state.sessions() as db:
                    await db.execute(text("SELECT memory_scope, organization_id FROM research_jobs LIMIT 1"))
                    await db.execute(text("SELECT embedding, organization_id FROM memory_entries LIMIT 1"))
                checks["postgres"] = True
        except Exception:
            pass
        try:
            checks["redis"] = bool(await app.state.redis.ping())
        except Exception:
            pass
        return checks

    def operator(request):
        expected = cfg.metrics_token.get_secret_value()
        if not expected or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {expected}"):
            raise AppError("UNAUTHORIZED", "Operator credentials required", 401)

    @app.get("/live")
    async def live():
        return {"status": "alive"}

    @app.get("/ready")
    async def ready():
        healthy = all((await diagnostics()).values())
        return JSONResponse({"status": "ready" if healthy else "unavailable"}, 200 if healthy else 503)

    @app.get("/health")
    async def health(request: Request):
        operator(request)
        checks = await diagnostics()
        return JSONResponse(checks, 200 if all(checks.values()) else 503)

    @app.get("/metrics")
    async def metrics(request: Request):
        operator(request)
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.include_router(auth.router)
    app.include_router(research.router)
    app.include_router(memory.router)
    return app


app = create_app()
