import asyncio
import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.core.errors import AppError
from app.core.rate_limit import rate_limit
from app.db.models import AuthSession, User

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
COOKIE = "mars_session"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or len(token) > 200:
        raise AppError("UNAUTHORIZED", "Sign in to continue", 401)
    await rate_limit(
        request.app.state.redis, request.client.host if request.client else "unknown", "authenticated-ip", 600
    )
    async with request.app.state.sessions() as db:
        session = await db.get(AuthSession, token_hash(token))
        if not session or session.expires_at <= time.time():
            raise AppError("UNAUTHORIZED", "Session expired; sign in again", 401)
        owner = session.user_id
    request.state.user_id = owner
    await rate_limit(
        request.app.state.redis,
        owner,
        getattr(request.scope.get("route"), "path", "unknown"),
        request.app.state.settings.api_rate_per_minute,
    )
    return owner


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    await rate_limit(request.app.state.redis, request.client.host if request.client else "unknown", "login", 10)
    await rate_limit(request.app.state.redis, body.email.lower(), "login-account", 10)
    async with request.app.state.sessions.begin() as db:
        user = await db.scalar(select(User).where(User.email == body.email.strip().lower()))
        valid = False
        try:
            digest = user.password_hash if user else request.app.state.dummy_password_hash
            valid = await asyncio.to_thread(hasher.verify, digest, body.password)
        except VerificationError:
            valid = False
        if not valid or not user:
            raise AppError("UNAUTHORIZED", "Invalid email or password", 401)
        token = secrets.token_urlsafe(48)
        seconds = request.app.state.settings.session_days * 86400
        await db.execute(
            delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at < time.time())
        )
        db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=time.time() + seconds))
        response.set_cookie(
            COOKIE,
            token,
            max_age=seconds,
            httponly=True,
            secure=request.app.state.settings.environment in {"production", "staging"},
            samesite="strict",
            path="/",
        )
        return {"id": user.id, "email": user.email}


@router.get("/me")
async def me(request: Request, owner: str = Depends(current_user)):
    async with request.app.state.sessions() as db:
        user = await db.get(User, owner)
        return {"id": user.id, "email": user.email}


@router.get("/organizations")
async def organizations(request: Request, owner: str = Depends(current_user)):
    return await request.app.state.repo.organizations(owner)


@router.post("/logout")
async def logout(request: Request, response: Response, owner: str = Depends(current_user)):
    async with request.app.state.sessions.begin() as db:
        await db.execute(delete(AuthSession).where(AuthSession.user_id == owner))
    response.delete_cookie(COOKIE, path="/")
    return {"status": "signed_out"}
