"""AuthN tests: password + email-OTP login (D1), registration email
verification, password reset, and the session lifecycle.

Integration tests against the local emulators (Firebase :9099) and the dev
database — same httpx.ASGITransport single-loop harness as the other
identity tests. OTP codes are read via the flag-gated dev endpoint
(SEAVIX_DEV_OTP_ENDPOINT=1, set in the root conftest).
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


async def _register(c, slug, email, password="secret-123", kind="survey_company"):
    return await c.post(
        "/api/register",
        json={
            "org_name": "Authn Org",
            "org_type": "marine",
            "slug": slug,
            "email": email,
            "password": password,
            "account_kind": kind,
        },
    )


async def _dev_code(c, email) -> str:
    resp = await c.get("/api/dev/last-otp", params={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def _login(c, email, code) -> httpx.Response:
    return await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )


def _session(sid) -> dict:
    return {"X-Seavix-Session": sid}


@pytest.fixture()
def client():
    async def start():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    return start


def test_login_happy_path_and_logout(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"au-{u}", f"au-{u}@example.com"
            await _register(c, slug, email)

            start = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            assert start.status_code == 200
            assert start.json()["otp_sent"] is True

            code = await _dev_code(c, email)
            done = await _login(c, email, code)
            assert done.status_code == 200
            body = done.json()
            sid = body["session_id"]
            assert body["email"] == email
            assert body["email_verified"] is False  # registration verifies separately
            assert body["memberships"][0]["slug"] == slug
            assert body["memberships"][0]["type"] == "survey_company"

            # session usable
            me = await c.get("/api/auth/session", headers=_session(sid))
            assert me.status_code == 200
            assert me.json()["email"] == email
            assert me.json()["memberships"][0]["org_id"]

            # refresh extends it
            ref = await c.post(
                "/api/auth/session/refresh", json={}, headers=_session(sid)
            )
            assert ref.status_code == 200

            # logout revokes it
            out = await c.delete("/api/auth/session", headers=_session(sid))
            assert out.status_code == 200
            me2 = await c.get("/api/auth/session", headers=_session(sid))
            assert me2.status_code == 401

    arun(scenario())


def test_login_start_rejects_bad_credentials_and_unknown_email(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"lc-{u}", f"lc-{u}@example.com"
            await _register(c, slug, email)

            wrong_pw = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "nope-nope"},
            )
            assert wrong_pw.status_code == 401
            unknown = await c.post(
                "/api/auth/login/start",
                json={"email": f"ghost-{u}@example.com", "password": "secret-123"},
            )
            assert unknown.status_code == 401
            # anti-enumeration: identical error shape
            assert wrong_pw.json() == unknown.json()

    arun(scenario())


def test_wrong_code_then_lockout_then_replay(arun, client):
    """D1: 5 attempts/code. Wrong codes burn attempts; the 5th failure
    invalidates the code (lockout) so even the right code afterwards fails;
    a code can never be replayed once consumed."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"lk-{u}", f"lk-{u}@example.com"
            await _register(c, slug, email)
            await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            code = await _dev_code(c, email)
            wrong = "000000" if code != "000000" else "000001"

            for i in range(5):
                r = await _login(c, email, wrong)
                assert r.status_code == 401, f"attempt {i + 1}"
            # lockout: right code now fails too
            assert (await _login(c, email, code)).status_code == 401

            # fresh code round-trips fine (no cross-code punishment)
            await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            code2 = await _dev_code(c, email)
            ok = await _login(c, email, code2)
            assert ok.status_code == 200
            # replay of the consumed code
            assert (await _login(c, email, code2)).status_code == 401

    arun(scenario())


def test_expired_code_rejected(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"ex-{u}", f"ex-{u}@example.com"
            await _register(c, slug, email)
            await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            code = await _dev_code(c, email)

            async with SessionLocal() as s:
                from sqlalchemy import text

                await s.execute(
                    text(
                        "UPDATE email_otp SET expires_at = :past "
                        "WHERE email = :email AND consumed_at IS NULL"
                    ),
                    {
                        "past": datetime.now(timezone.utc) - timedelta(minutes=1),
                        "email": email,
                    },
                )
                await s.commit()

            assert (await _login(c, email, code)).status_code == 401

    arun(scenario())


def test_send_rate_limit_5_per_hour(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"rl-{u}", f"rl-{u}@example.com"
            await _register(c, slug, email)
            results = []
            for _ in range(6):
                r = await c.post(
                    "/api/register/verify/send", json={"email": email}
                )
                results.append(r.status_code)
            assert results[:5] == [200] * 5
            assert results[5] == 429

    arun(scenario())


def test_registration_email_verification(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"ve-{u}", f"ve-{u}@example.com"
            await _register(c, slug, email)

            # wrong code first (deliberately different from the issued one)
            await c.post("/api/register/verify/send", json={"email": email})
            code = await _dev_code(c, email)
            wrong = "000000" if code != "000000" else "000001"
            bad = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "code": wrong},
            )
            assert bad.status_code == 401

            ok = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "code": code},
            )
            assert ok.status_code == 200
            assert ok.json()["email_verified"] is True

            # replay of the consumed code fails
            again = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "code": code},
            )
            assert again.status_code == 401

            # verified flag visible on the session context
            await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            code2 = await _dev_code(c, email)
            done = await _login(c, email, code2)
            assert done.json()["email_verified"] is True

            # unknown email cannot request codes
            ghost = await c.post(
                "/api/register/verify/send",
                json={"email": f"ghost-{u}@example.com"},
            )
            assert ghost.status_code == 404

    arun(scenario())


def test_password_reset_round_trip(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"pr-{u}", f"pr-{u}@example.com"
            await _register(c, slug, email)

            start = await c.post(
                "/api/auth/reset/start", json={"email": email}
            )
            assert start.status_code == 200
            assert start.json() == {"sent": True}

            # The emulator neither sends email nor returns the link on the
            # REST response; its inspection API is the dev "mailbox".
            async with httpx.AsyncClient(base_url="http://localhost:9099") as fc:
                resp = await fc.get("/emulator/v1/projects/seavix-dev/oobCodes")
            entries = [
                e for e in resp.json()["oobCodes"]
                if e["email"] == email and e["requestType"] == "PASSWORD_RESET"
            ]
            link = entries[-1]["oobLink"]
            oob = dict(
                kv.split("=", 1)
                for kv in link.split("?", 1)[1].split("&")
            )["oobCode"]

            bad = await c.post(
                "/api/auth/reset/finish",
                json={"oob_code": "bogus-code", "new_password": "new-secret-1"},
            )
            assert bad.status_code == 400

            fin = await c.post(
                "/api/auth/reset/finish",
                json={"oob_code": oob, "new_password": "new-secret-1"},
            )
            assert fin.status_code == 200

            # old password dead, new password works
            old = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            assert old.status_code == 401
            new = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "new-secret-1"},
            )
            assert new.status_code == 200

    arun(scenario())


def test_garbage_session_and_missing_header_401(arun, client):
    async def scenario():
        async with await client() as c:
            no_header = await c.get("/api/auth/session")
            assert no_header.status_code == 401
            garbage = await c.get(
                "/api/auth/session",
                headers={"X-Seavix-Session": str(uuid.uuid4())},
            )
            assert garbage.status_code == 401
            # dev endpoint inert for unknown emails
            empty = await c.get(
                "/api/dev/last-otp", params={"email": f"none-{uuid.uuid4().hex[:8]}@example.com"}
            )
            assert empty.status_code == 404

    arun(scenario())
