"""Email OTP — the custom second factor (D1).

6-digit code, 10-minute TTL, single-use, hashed at rest, attempt-limited
(5 tries/code). NO send quota: owner decision 24 Oct removed all per-email
send throttling — login OTP sends, resends and verification-link sends are
unlimited. Firebase has no native email-OTP, so
the identity service owns the lifecycle: generation, delivery through the
ONE mail pipeline (MailProvider port; dev = LogMailSender, prod = real
provider — see the email-pipeline task report), plus a flag-gated dev peek
(/api/dev/last-otp) for tests and local development.

The code is NEVER stored or logged by Firebase, so the firebase-auth
container logs cannot show it — dev readability lives here by design.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import config

PURPOSES = ("login", "verify_email")

# Dev-only plaintext cache backing /api/dev/last-otp (flag-gated). Lives in
# process memory only — the database stores hashes (D1).
_DEV_CODES: dict[str, tuple[str, str]] = {}


def dev_last_code(email: str) -> tuple[str, str] | None:
    return _DEV_CODES.get(email.strip().lower())


def _hash(email: str, code: str) -> str:
    raw = f"{email.strip().lower()}:{code}:{config.otp_pepper()}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _log_send(
    session: AsyncSession, email: str, purpose: str, code_hash: str,
    ttl_seconds: int,
) -> None:
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
            "hash": code_hash,
            "now": now,
            "expires": now + timedelta(seconds=ttl_seconds),
        },
    )


async def send_code(session: AsyncSession, email: str, purpose: str) -> None:
    """Create and "send" a code. No send quota — sends and resends are
    unlimited (owner decision 24 Oct); only attempts per code are capped."""
    email = email.strip().lower()
    if purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail="unknown OTP purpose")

    code = f"{secrets.randbelow(1_000_000):06d}"
    await _log_send(session, email, purpose, _hash(email, code),
                    config.otp_ttl_seconds())
    await session.commit()
    # Delivery goes through the ONE mail pipeline (dev: LogMailSender prints
    # the email to the identity log; prod: real provider). The code also
    # lands in the flag-gated dev peek below.
    from .mail_templates import otp_login_email
    from .mailer import get_mailer

    subject, plain, html = otp_login_email(
        code, config.otp_ttl_seconds() // 60,
        purpose="verification" if purpose == "verify_email" else "sign-in",
    )
    await get_mailer().send(email, subject, plain, html)
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
