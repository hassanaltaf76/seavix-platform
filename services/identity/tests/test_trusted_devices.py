"""31j trusted-device MFA: the "Trust this device for 30 days" bypass
behind the email-OTP login.

Covers the owner-facing contract end to end at the identity API:
- OTP verify with trust_device enrols a token (returned once, sha256 at
  rest, 30-day expiry, user-agent recorded)
- login/start with a valid token skips the OTP and issues a session
  directly; the token is rotated (the old one dies, the new one works)
- invalid / expired / revoked tokens fall back to the normal OTP
- revoke-all kills every enrolment
- password reset revokes every trusted device of the user
- an unchecked challenge creates no enrolment (OTP every login)

Integration tests against the local emulators and the dev database, same
single-loop ASGI harness as test_authn.py; OTP codes via the flag-gated
dev endpoint (set in the root conftest).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402


async def _register(c, email, password="secret-123"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Trusted Device Org",
            "org_type": "survey_company",
            "email": email,
            "password": password,
            "confirm_password": password,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _verify_email(email: str) -> None:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _dev_code(c, email) -> str:
    resp = await c.get("/api/dev/last-otp", params={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def _login_with_otp(c, email, *, trust_device=False,
                          user_agent="pytest-identity") -> dict:
    """Full two-factor login; returns the login/verify body."""
    start = await c.post(
        "/api/auth/login/start",
        json={"email": email, "password": "secret-123"},
    )
    assert start.status_code == 200, start.text
    assert start.json()["otp_sent"] is True
    code = await _dev_code(c, email)
    body: dict = {"email": email, "code": code}
    if trust_device:
        body["trust_device"] = True
        body["device_user_agent"] = user_agent
    verified = await c.post("/api/auth/login/verify", json=body)
    assert verified.status_code == 200, verified.text
    return verified.json()


async def _trusted_rows(email: str) -> list[dict]:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                sql(
                    "SELECT t.token_hash, t.user_agent, t.expires_at, "
                    "t.revoked_at, length(t.token_hash) AS hash_len "
                    "FROM trusted_device t "
                    "JOIN app_user u ON u.id = t.user_id "
                    "WHERE u.email = :e ORDER BY t.created_at"
                ),
                {"e": email},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _backdate_expiry(email: str) -> None:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        await s.execute(
            sql(
                "UPDATE trusted_device SET expires_at = :past "
                "WHERE user_id = (SELECT id FROM app_user WHERE email = :e)"
            ),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1),
             "e": email},
        )
        await s.commit()


@pytest.fixture()
def client():
    async def start():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    return start


def test_trusted_device_lifecycle_skip_otp_and_rotation(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"td-{uuid.uuid4().hex[:8]}@example.com"
            await _register(c, email)
            await _verify_email(email)

            # unchecked challenge: session, NO trusted token
            plain = await _login_with_otp(c, email)
            assert "trusted_device" not in plain
            assert await _trusted_rows(email) == []

            # trusted challenge: token returned once
            trusted = await _login_with_otp(
                c, email, trust_device=True,
                user_agent="pytest-agent/1.0",
            )
            token = trusted.get("trusted_device")
            assert token and "." in token
            rows = await _trusted_rows(email)
            assert len(rows) == 1
            assert rows[0]["user_agent"] == "pytest-agent/1.0"
            assert rows[0]["revoked_at"] is None
            assert rows[0]["hash_len"] == 64  # sha256 hex
            assert rows[0]["expires_at"] > datetime.now(timezone.utc)

            # password-only login with the token: OTP skipped, session now
            skipped = await c.post(
                "/api/auth/login/start",
                json={
                    "email": email,
                    "password": "secret-123",
                    "trusted_device": token,
                    "device_user_agent": "pytest-agent/1.0",
                },
            )
            assert skipped.status_code == 200, skipped.text
            body = skipped.json()
            assert body["otp_sent"] is False
            assert body["session_id"]
            assert body["memberships"][0]["slug"]

            # rotation: the presented token is dead, the rotated one works
            rotated = body["trusted_device"]
            assert rotated and rotated != token
            replay = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123",
                      "trusted_device": token},
            )
            assert replay.status_code == 200
            assert replay.json()["otp_sent"] is True
            assert replay.json()["trusted_device_valid"] is False

    arun(scenario())


def test_expired_and_revoked_tokens_fall_back_to_otp(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"td-{uuid.uuid4().hex[:8]}@example.com"
            await _register(c, email)
            await _verify_email(email)
            token = (await _login_with_otp(c, email, trust_device=True))[
                "trusted_device"
            ]

            # expiry -> OTP again
            await _backdate_expiry(email)
            expired = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123",
                      "trusted_device": token},
            )
            assert expired.status_code == 200
            assert expired.json()["otp_sent"] is True

            # fresh enrolment, then revoke-all via the API
            fresh = (await _login_with_otp(c, email, trust_device=True))[
                "trusted_device"
            ]
            sid_resp = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123",
                      "trusted_device": fresh,
                      "device_user_agent": "pytest-agent/rotated"},
            )
            sid = sid_resp.json()["session_id"]
            listing = await c.get(
                "/api/auth/trusted-devices",
                headers={"X-Seavix-Session": sid},
            )
            assert listing.status_code == 200
            assert listing.json()["count"] == 1
            # the rotated enrolment carries the UA forwarded at login/start
            assert listing.json()["devices"][0]["user_agent"] == "pytest-agent/rotated"

            revoked = await c.delete(
                "/api/auth/trusted-devices",
                headers={"X-Seavix-Session": sid},
            )
            assert revoked.status_code == 200
            assert revoked.json()["revoked"] >= 1

            # revoked token -> OTP again
            after = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123",
                      "trusted_device": fresh},
            )
            assert after.status_code == 200
            assert after.json()["otp_sent"] is True
            assert after.json()["trusted_device_valid"] is False

    arun(scenario())


def test_password_reset_revokes_all_trusted_devices(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"td-{uuid.uuid4().hex[:8]}@example.com"
            await _register(c, email)
            await _verify_email(email)
            token = (await _login_with_otp(c, email, trust_device=True))[
                "trusted_device"
            ]

            start = await c.post("/api/auth/reset/start", json={"email": email})
            assert start.status_code == 200
            from app.mailer import get_mailer

            oob = None
            for m in reversed(get_mailer().outbox):
                if m.to == email and m.subject.startswith(
                    "Reset your SeaVix password"
                ):
                    from urllib.parse import parse_qs, urlparse

                    for line in m.body_text.splitlines():
                        if "oob_code=" in line:
                            oob = parse_qs(urlparse(line.strip()).query)[
                                "oob_code"
                            ][0]
            assert oob
            fin = await c.post(
                "/api/auth/reset/finish",
                json={"oob_code": oob, "new_password": "new-secret-1"},
            )
            assert fin.status_code == 200

            # every enrolment is revoked: the old token must NOT skip OTP
            replay = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "new-secret-1",
                      "trusted_device": token},
            )
            assert replay.status_code == 200
            assert replay.json()["otp_sent"] is True
            assert replay.json()["trusted_device_valid"] is False

    arun(scenario())


def test_unchecked_login_needs_otp_every_time(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"td-{uuid.uuid4().hex[:8]}@example.com"
            await _register(c, email)
            await _verify_email(email)
            for _ in range(2):
                body = await _login_with_otp(c, email)  # box never checked
                assert "trusted_device" not in body
            # the second factor fired every time: two consumed codes exist
            assert await _trusted_rows(email) == []

    arun(scenario())
