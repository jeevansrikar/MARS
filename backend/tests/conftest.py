import os
import time

import fakeredis.aioredis
import httpx
import pytest
from redis.asyncio import Redis

from app.api.auth import token_hash
from app.core.config import Settings
from app.db.models import AuthSession, Base, User
from app.db.repository import Repository
from app.db.session import make_database
from app.main import create_app


@pytest.fixture
async def system(tmp_path):
    cfg = Settings(
        environment="testing",
        database_url=os.getenv("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"),
        anthropic_api_key="test-provider",
        search_api_key="test-search",
        metrics_token="test-operator-token",
        max_active_jobs=3,
        semantic_enabled=False,
    )
    engine, sessions = make_database(cfg)
    async with engine.begin() as connection:
        if cfg.database_url.startswith("postgresql"):
            from sqlalchemy import text

            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)
    redis = (
        Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
        if os.getenv("TEST_REDIS_URL")
        else fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    async with sessions.begin() as db:
        a, b = User(email="a@example.org", password_hash="unused"), User(email="b@example.org", password_hash="unused")
        db.add_all([a, b])
        await db.flush()
        db.add_all(
            [
                AuthSession(user_id=a.id, token_hash=token_hash("token-a"), expires_at=time.time() + 600),
                AuthSession(user_id=b.id, token_hash=token_hash("token-b"), expires_at=time.time() + 600),
            ]
        )
    app = create_app(cfg, sessions=sessions, redis=redis)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer token-a", "X-MARS-Client": "1"},
        ) as client:
            yield app, client, Repository(sessions, cfg), redis, a.id, b.id
    await redis.aclose()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
