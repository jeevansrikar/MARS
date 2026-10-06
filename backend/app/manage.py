"""Operator commands; no public registration or unauthenticated administrative endpoints."""

import argparse
import asyncio
import getpass
import time

from sqlalchemy import delete, select

from app.api.auth import hasher
from app.core.config import get_settings
from app.db.models import AuditRecord, AuthSession, Membership, MemoryRecord, Organization, User
from app.db.session import make_database


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["create-user", "reset-password", "delete-user", "prune", "create-org", "add-member", "remove-member"],
    )
    parser.add_argument("--email")
    parser.add_argument("--name")
    parser.add_argument("--organization")
    args = parser.parse_args()
    engine, sessions = make_database(get_settings())
    try:
        async with sessions.begin() as db:
            if args.command == "create-org":
                if not args.name or len(args.name) > 200:
                    parser.error("--name (1-200 characters) is required")
                organization = Organization(name=args.name)
                db.add(organization)
                await db.flush()
                print(organization.id)
                return
            if args.command == "prune":
                await db.execute(delete(MemoryRecord).where(MemoryRecord.expires_at < time.time()))
                await db.execute(delete(AuthSession).where(AuthSession.expires_at < time.time()))
                return
            if not args.email or "@" not in args.email:
                parser.error("--email is required")
            email = args.email.strip().lower()
            user = await db.scalar(select(User).where(User.email == email).with_for_update())
            if args.command in {"add-member", "remove-member"}:
                if not user or not args.organization or not await db.get(Organization, args.organization):
                    parser.error("An existing user and --organization ID are required")
                member = await db.get(Membership, (args.organization, user.id))
                if args.command == "add-member" and not member:
                    db.add(Membership(organization_id=args.organization, user_id=user.id))
                if args.command == "remove-member" and member:
                    await db.delete(member)
                db.add(AuditRecord(user_id=user.id, action=args.command, target_id=args.organization))
                return
            if args.command == "delete-user":
                if user:
                    await db.delete(user)
            else:
                password = getpass.getpass("New password (at least 12 characters): ")
                if len(password) < 12 or len(password) > 256:
                    parser.error("Password must contain 12-256 characters")
                digest = await asyncio.to_thread(hasher.hash, password)
                if args.command == "create-user":
                    if user:
                        parser.error("User already exists")
                    db.add(User(email=email, password_hash=digest))
                else:
                    if not user:
                        parser.error("User does not exist")
                    user.password_hash, user.updated_at = digest, time.time()
                    await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
