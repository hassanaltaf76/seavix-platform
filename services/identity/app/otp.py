"""Email OTP — the custom second factor (D1).

6-digit code, 10-minute TTL, single-use, hashed at rest, rate-limited
(5 sends/hour/email, 5 attempts/code). Firebase has no native email-OTP, so
the identity service owns the whole lifecycle: generation, "delivery"
(prod email wiring = TODO; dev logs the code and, behind an explicit flag,
exposes it via /api/dev/last-otp), verification, and expiry.

The code is NEVER stored or logged by Firebase, so the firebase-auth
container logs cannot show it — dev readability lives here by design.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import config

logger = logging.getLogger("seavix.identity.otp")

PURPOSES = ("login", "verify_email")

# Dev-only plaintext cache backing /api/dev/last-otp (flag-gated). Lives in
# process memory only — the database stores hashes (D1).
_DEV_CODES: dict[str, tuple[str, str]] = {}


def dev_last_code(email: str) -> tuple[str, str] | None:
    return _DEV_CODES.get(email.strip().lower())


def _hash(email: str, code: str) -> str:
    raw = f"{email.strip().lower()}:{code}:{config.otp_pepper()}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def send_code(session: AsyncSession, email: str, purpose: str) -> None:
    """Create and "send" a code. Enforces the per-email send window."""
    email = email.strip().lower()
    if purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail="unknown OTP purpose")

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)
    sent = (
        await session.execute(
            text(
                "SELECT count(*) FROM email_otp "
                "WHERE email = :email AND created_at > :window"
            ),
            {"email": email, "window": window_start},
        )
    ).scalar_one()
    if sent >= config.otp_send_limit_per_hour():
        raise HTTPException(
            status_code=429, detail="too many codes requested — try again later"
        )

    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "INSERT INTO email_otp (id, email, purpose, code_hash, created_at, expires_at) "
            "VALUES (:id, :email, :purpose, :hash, :now, :expires)"
        ),
        {
            "id": str(uuid.uuid4()),
            "email": email,
            "purpose": purpose,
            "hash": _hash(email, code),
            "now": now,
            "expires": now + timedelta(seconds=config.otp_ttl_seconds()),
        },
    )
    await session.commit()
    # Dev delivery channel (D1). Prod: send via email provider — TODO.
    logger.info("DEV-OTP email=%s purpose=%s code=%s", email, purpose, code)
    if config.dev_otp_endpoint_enabled():
        _DEV_CODES[email] = (purpose, code)


async def verify_code(session: AsyncSession, email: str, purpose: str, code: str) -> None:
    """Consume a code: single-use, TTL-bounded, attempt-limited.

    Raises 401 for any failure (wrong/expired/unknown) so the endpoint layer
    stays enum-free; replay and lockout are distinguishable in tests via the
    consumed_at/attempts columns, not via distinct error codes — an attacker
    must not learn which step failed.
    """
    email = email.strip().lower()
    row = (
        await session.execute(
            text(
                "SELECT id, code_hash, expires_at, consumed_at, attempts "
                "FROM email_otp WHERE email = :email AND purpose = :purpose "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"email": email, "purpose": purpose},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid code")

    now = datetime.now(timezone.utc)
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if row["consumed_at"] is not None:
        raise HTTPException(status_code=401, detail="invalid code")  # replay
    if now > expires_at:
        raise HTTPException(status_code=401, detail="invalid code")  # expired
    if row["attempts"] >= config.otp_attempt_limit():
        # Lockout: burn the code so it can never succeed after N failures.
        await session.execute(
            text("UPDATE email_otp SET consumed_at = :now WHERE id = :id"),
            {"now": now, "id": row["id"]},
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="invalid code")

    if row["code_hash"] != _hash(email, code):
        await session.execute(
            text("UPDATE email_otp SET attempts = attempts + 1 WHERE id = :id"),
            {"id": row["id"]},
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="invalid code")

    await session.execute(
        text("UPDATE email_otp SET consumed_at = :now WHERE id = :id"),
        {"now": now, "id": row["id"]},
    )
    await session.commit()


async def last_code_for_dev(session: AsyncSession, email: str) -> dict | None:
    """Most recent code for an email — dev endpoint only (flag-gated)."""
    row = (
        await session.execute(
            text(
                "SELECT purpose, code_hash, created_at, expires_at, consumed_at, attempts "
                "FROM email_otp WHERE email = :email ORDER BY created_at DESC LIMIT 1"
            ),
            {"email": email.strip().lower()},
        )
    ).mappings().first()
    return dict(row) if row else None
