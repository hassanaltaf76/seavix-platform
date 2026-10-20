"""Trusted devices (task 31j) — the "remember this device for 30 days"
second-factor bypass behind the email-OTP login.

Token shape: "<uuid4>.<urlsafe-secret>". The cookie carries the full
token; the database stores ONLY sha256(token) (D1 posture — same as the
OTP codes and sessions). A token is bound to one user, lives 30 days,
and is rotated on every trusted login so an active user's window rolls
forward instead of hard-stopping on day 30. Password reset revokes every
device of the user; logout deliberately keeps trust.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TRUST_DAYS = 30
_USER_AGENT_MAX = 300


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def issue(session: AsyncSession, user_id: str, user_agent: str) -> str:
    """Enrol a new trusted device; returns the opaque cookie token."""
    token = f"{uuid.uuid4()}.{secrets.token_urlsafe(32)}"
    now = _now()
    ua = (user_agent or "")[:_USER_AGENT_MAX]
    await session.execute(
        text(
            "INSERT INTO trusted_device (id, user_id, token_hash, user_agent, "
            "created_at, expires_at, last_seen_at) "
            "VALUES (:id, :uid, :hash, :ua, :now, :expires, :now)"
        ),
        {
            "id": str(uuid.uuid4()),
            "uid": user_id,
            "hash": _hash(token),
            "ua": ua,
            "now": now,
            "expires": now + timedelta(days=TRUST_DAYS),
        },
    )
    await session.commit()
    return token


async def consume(
    session: AsyncSession, user_id: str, token: str, user_agent: str = "",
) -> tuple[bool, str | None]:
    """Validate a presented token for the user. On success rotates it
    (the presented row is revoked, a fresh token with a fresh 30-day
    window is returned) so daily users never hit a hard 30-day cliff.

    Returns (ok, new_token). Any failure — unknown hash, revoked,
    expired, or a token belonging to another user — is (False, None):
    the caller falls back to the normal OTP challenge.
    """
    token = (token or "").strip()
    if not token or "." not in token:
        return False, None
    row = (
        await session.execute(
            text(
                "SELECT id, expires_at, revoked_at FROM trusted_device "
                "WHERE token_hash = :hash AND user_id = :uid"
            ),
            {"hash": _hash(token), "uid": user_id},
        )
    ).mappings().first()
    if row is None:
        return False, None
    now = _now()
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if row["revoked_at"] is not None or now > expires_at:
        return False, None
    await session.execute(
        text("UPDATE trusted_device SET revoked_at = :now WHERE id = :id"),
        {"now": now, "id": str(row["id"])},
    )
    await session.commit()
    return True, await issue(session, user_id, user_agent=user_agent)


async def revoke_all(session: AsyncSession, user_id: str) -> int:
    """Revoke every trusted device of the user (password reset/change and
    the account surface's 'sign out all trusted devices')."""
    now = _now()
    result = await session.execute(
        text(
            "UPDATE trusted_device SET revoked_at = :now "
            "WHERE user_id = :uid AND revoked_at IS NULL"
        ),
        {"now": now, "uid": user_id},
    )
    await session.commit()
    return result.rowcount


async def count_active(session: AsyncSession, user_id: str) -> int:
    now = _now()
    return (
        await session.execute(
            text(
                "SELECT count(*) FROM trusted_device "
                "WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > :now"
            ),
            {"uid": user_id, "now": now},
        )
    ).scalar_one()


async def list_active(session: AsyncSession, user_id: str) -> list[dict]:
    """Display metadata for the account surface — never security input."""
    now = _now()
    rows = (
        await session.execute(
            text(
                "SELECT created_at, last_seen_at, expires_at, user_agent "
                "FROM trusted_device "
                "WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > :now "
                "ORDER BY last_seen_at DESC"
            ),
            {"uid": user_id, "now": now},
        )
    ).mappings().all()

    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    return [
        {
            "created_at": _iso(r["created_at"]),
            "last_seen_at": _iso(r["last_seen_at"]),
            "expires_at": _iso(r["expires_at"]),
            "user_agent": r["user_agent"],
        }
        for r in rows
    ]
