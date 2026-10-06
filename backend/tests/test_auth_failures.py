import time
from unittest.mock import AsyncMock

from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.auth import hasher
from app.db.models import AuthSession, User


async def test_login_cookie_logout_revokes_sessions(system):
    _, client, repo, _, owner, _ = system
    async with repo.sessions.begin() as db:
        user = await db.get(User, owner)
        user.password_hash = hasher.hash("a-test-password-long-enough")
    invalid = await client.post("/api/v1/auth/login", json={"email": "a@example.org", "password": "wrong"})
    assert invalid.status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.org", "password": "a-test-password-long-enough"}
    )
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": ""})).json()["id"] == owner
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_expired_credentials_and_redis_outage_fail_closed(system, monkeypatch):
    _, client, repo, redis, _, _ = system
    monkeypatch.setattr(redis, "eval", AsyncMock(side_effect=RedisConnectionError("private infrastructure detail")))
    response = await client.get("/api/v1/research")
    assert response.status_code == 503
    assert "private infrastructure" not in response.text
    monkeypatch.undo()
    async with repo.sessions.begin() as db:
        from app.api.auth import token_hash

        session = await db.get(AuthSession, token_hash("token-a"))
        session.expires_at = time.time() - 1
    assert (await client.get("/api/v1/research")).status_code == 401


async def test_database_outage_changes_readiness(system, monkeypatch):
    app, client, _, _, _, _ = system

    def unavailable():
        raise ConnectionError("private database connection")

    monkeypatch.setattr(app.state, "sessions", unavailable)
    response = await client.get("/ready")
    assert response.status_code == 503 and "private" not in response.text
    assert (await client.get("/live")).status_code == 200
