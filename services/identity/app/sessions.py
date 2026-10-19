"""Opaque server-side sessions.

The web app's HttpOnly cookie carries only the session id. Everything
security-relevant (revocation, member deactivation, expiry) is evaluated
against this table on every request, so logout and deactivation take effect
immediately — a stateless JWT cookie could not promise that.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import config


async def create_session(session: AsyncSession, user_id: str, firebase_uid: str) -> str:
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "INSERT INTO auth_session (id, user_id, firebase_uid, created_at, expires_at, last_seen_at) "
            "VALUES (:id, :uid, :fb, :now, :expires, :now)"
        ),
        {
            "id": sid,
            "uid": user_id,
            "fb": firebase_uid,
            "now": now,
            "expires": now + timedelta(seconds=config.session_ttl_seconds()),
        },
    )
    await session.commit()
    return sid


async def load_session(session: AsyncSession, sid: str) -> dict:
    """Load a live session, sliding its expiry. 401 on anything dead."""
    row = (
        await session.execute(
            text(
                "SELECT s.id, s.user_id, s.firebase_uid, s.expires_at, s.revoked_at, "
                "u.email, u.email_verified "
                "FROM auth_session s JOIN app_user u ON u.id = s.user_id "
                "WHERE s.id = :id"
            ),
            {"id": sid},
        )
    ).mappings().first()
    now = datetime.now(timezone.utc)
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="invalid session")
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        raise HTTPException(status_code=401, detail="session expired")
    new_expiry = now + timedelta(seconds=config.session_ttl_seconds())
    await session.execute(
        text(
            "UPDATE auth_session SET last_seen_at = :now, expires_at = :expiry WHERE id = :id"
        ),
        {"now": now, "expiry": new_expiry, "id": sid},
    )
    await session.commit()
    return {
        "session_id": sid,
        "user_id": str(row["user_id"]),
        "firebase_uid": row["firebase_uid"],
        "email": row["email"],
        "email_verified": bool(row["email_verified"]),
    }


async def revoke_session(session: AsyncSession, sid: str) -> bool:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        text(
            "UPDATE auth_session SET revoked_at = :now WHERE id = :id AND revoked_at IS NULL"
        ),
        {"now": now, "id": sid},
    )
    await session.commit()
    return result.rowcount > 0


async def revoke_all_for_user(session: AsyncSession, user_id: str) -> int:
    """Used on member deactivation: every session of the user dies at once."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        text(
            "UPDATE auth_session SET revoked_at = :now "
            "WHERE user_id = :uid AND revoked_at IS NULL"
        ),
        {"now": now, "uid": user_id},
    )
    await session.commit()
    return result.rowcount
