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


async def _register(c, email, password="secret-123", org_type="survey_company"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Authn Org",
            "org_type": org_type,
            "email": email,
            "password": password,
            "confirm_password": password,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _dev_code(c, email) -> str:
    resp = await c.get("/api/dev/last-otp", params={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def _verify_email(c, email):
    """31i: login gates on a verified email. The helper marks the address
    verified directly in the dev DB (no mail send); the real link pipeline
    is covered by the dedicated verification tests below."""
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _login(c, email, code) -> httpx.Response:
    return await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )


def _session(sid) -> dict:
    return {"X-Seavix-Session": sid}


async def _last_mail(email: str, subject_prefix: str):
    """Newest outbox message for (recipient, subject prefix) — the mail
    pipeline IS the dev mailbox."""
    from app.mailer import get_mailer

    outbox = get_mailer().outbox
    for m in reversed(outbox):
        if m.to == email and m.subject.startswith(subject_prefix):
            return m
    raise AssertionError(f"no mail to {email} matching {subject_prefix!r}")


def _oob_from_body(body: str) -> str:
    from urllib.parse import parse_qs, urlparse

    for line in body.splitlines():
        if "oob_code=" in line:
            return parse_qs(urlparse(line.strip()).query)["oob_code"][0]
    raise AssertionError("no oob_code link in body")


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
            email = f"au-{u}@example.com"

            slug = (await _register(c, email))["slug"]
            await _verify_email(c, email)  # 31i: login requires verified

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
            # 31i invariant: a signed-in user is ALWAYS verified
            assert body["email_verified"] is True
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
            email = f"lc-{u}@example.com"

            slug = (await _register(c, email))["slug"]

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


def test_unverified_email_never_completes_login_31i(arun, client):
    """31i invariant: an unverified email NEVER completes login.

    - login/start with the CORRECT password: 403 email_unverified, and NO
      OTP is issued (the dev peek finds no code);
    - login/resend issues no sign-in code for the unverified address
      (constant response, but nothing sent);
    - even with a code in hand, login/verify refuses before consuming it
      (defense in depth) — no session is ever created;
    - verifying the address restores the normal OTP flow."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"gate-{u}@example.com"
            await _register(c, email)

            start = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            assert start.status_code == 403
            assert start.json()["detail"] == {
                "code": "email_unverified", "email": email,
            }
            peek = await c.get("/api/dev/last-otp", params={"email": email})
            assert peek.status_code == 404  # no OTP was issued

            # resend stays enum-free but sends nothing
            resend = await c.post(
                "/api/auth/login/resend", json={"email": email}
            )
            assert resend.status_code == 200
            peek2 = await c.get("/api/dev/last-otp", params={"email": email})
            assert peek2.status_code == 404

            # wrong-password posture is unchanged (401, not the gate)
            wrong = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "nope-nope"},
            )
            assert wrong.status_code == 401

            # verify through the REAL link pipeline, then the normal flow
            # works end to end
            sent = await c.post(
                "/api/register/verify/send", json={"email": email}
            )
            assert sent.status_code == 200, sent.text
            mail = await _last_mail(email, "Verify your SeaVix")
            oob = _oob_from_body(mail.body_text)
            confirmed = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "oob_code": oob},
            )
            assert confirmed.status_code == 200, confirmed.text
            ok = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            assert ok.status_code == 200
            code = await _dev_code(c, email)
            done = await _login(c, email, code)
            assert done.status_code == 200
            assert done.json()["email_verified"] is True

    arun(scenario())


def test_wrong_code_then_lockout_then_replay(arun, client):
    """D1: 5 attempts/code. Wrong codes burn attempts; the 5th failure
    invalidates the code (lockout) so even the right code afterwards fails;
    a code can never be replayed once consumed."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"lk-{u}@example.com"

            slug = (await _register(c, email))["slug"]
            await _verify_email(c, email)  # 31i: login requires verified
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
            email = f"ex-{u}@example.com"

            slug = (await _register(c, email))["slug"]
            await _verify_email(c, email)  # 31i: login requires verified
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


def test_login_otp_sends_and_resends_are_unlimited(arun, client):
    """Owner decision 24 Oct: NO per-email send quota. Eight login/start
    sends in a row all succeed, resends are unlimited too, no send path
    returns 429 from historical volume, and the newest code still logs in."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"rl-{u}@example.com"

            await _register(c, email)
            await _verify_email(c, email)  # 31i: login requires verified
            for _ in range(8):
                r = await c.post(
                    "/api/auth/login/start",
                    json={"email": email, "password": "secret-123"},
                )
                assert r.status_code == 200, r.text
            for _ in range(3):
                r = await c.post("/api/auth/login/resend", json={"email": email})
                assert r.status_code == 200, r.text
            # the burst above blocked nothing: the newest code completes login
            code = await _dev_code(c, email)
            assert (await _login(c, email, code)).status_code == 200

    arun(scenario())


def test_verification_link_sends_are_unlimited_and_decoupled(arun, client):
    """Owner decision 24 Oct: verification-link sends have no quota and no
    shared OTP budget. Eight sends succeed for one address, the OTP pipeline
    for that same address is unaffected, and other addresses send freely."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"vr-{u}@example.com"
            await _register(c, email)
            await _verify_email(c, email)

            for _ in range(8):
                r = await c.post(
                    "/api/register/verify/send", json={"email": email}
                )
                assert r.status_code == 200, r.text

            # no shared budget: the OTP pipeline for the same address is fine
            login = await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            assert login.status_code == 200, login.text

            other = f"vo-{u}@example.com"
            await _register(c, other)
            await _verify_email(c, other)
            fresh = await c.post(
                "/api/register/verify/send", json={"email": other}
            )
            assert fresh.status_code == 200

    arun(scenario())


def test_registration_email_verification(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"ve-{u}@example.com"

            slug = (await _register(c, email))["slug"]

            # verification link arrives through the mail pipeline
            await c.post("/api/register/verify/send", json={"email": email})
            mail = await _last_mail(email, "Verify your SeaVix email address")
            oob = _oob_from_body(mail.body_text)

            # wrong code first
            bad = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "oob_code": "bogus-code"},
            )
            assert bad.status_code == 400

            ok = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "oob_code": oob},
            )
            assert ok.status_code == 200
            assert ok.json()["email_verified"] is True

            # replay of the consumed link fails
            again = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "oob_code": oob},
            )
            assert again.status_code == 400

            # verified flag visible on the session context
            await c.post(
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            code2 = await _dev_code(c, email)
            done = await _login(c, email, code2)
            assert done.json()["email_verified"] is True

            # unknown email cannot request links
            ghost = await c.post(
                "/api/register/verify/send",
                json={"email": f"ghost-{u}@example.com"},
            )
            assert ghost.status_code == 404

    arun(scenario())


def test_mail_pipeline_delivers_every_flow(arun, client):
    """OTP codes, verification links and reset links all flow through the
    ONE MailProvider (LogMailSender outbox in dev)."""
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"mp-{u}@example.com"

            slug = (await _register(c, email))["slug"]

            await c.post(  # verification link
                "/api/register/verify/send", json={"email": email}
            )
            await _verify_email(c, email)  # 31i: login requires verified
            await c.post(  # OTP (login start)
                "/api/auth/login/start",
                json={"email": email, "password": "secret-123"},
            )
            await c.post("/api/auth/reset/start", json={"email": email})

            verify = await _last_mail(email, "Verify your SeaVix")
            otp = await _last_mail(email, "Your SeaVix sign-in")
            reset = await _last_mail(email, "Reset your SeaVix")
            assert all(m.to == email for m in (verify, otp, reset))
            assert "oob_code=" in verify.body_text
            assert "oob_code=" in reset.body_text
            first_line = otp.body_text.strip().splitlines()[0]
            assert first_line.rstrip(".").rsplit(" ", 1)[-1].isdigit()  # the code

    arun(scenario())


def test_password_reset_round_trip(arun, client):
    async def scenario():
        async with await client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"pr-{u}@example.com"

            slug = (await _register(c, email))["slug"]

            start = await c.post(
                "/api/auth/reset/start", json={"email": email}
            )
            assert start.status_code == 200
            assert start.json() == {"sent": True}

            # The dev "mailbox" is the LogMailSender outbox.
            mail = await _last_mail(email, "Reset your SeaVix password")
            oob = _oob_from_body(mail.body_text)

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

            # 31i: verify the address — login gates on it — then the new
            # password works
            await _verify_email(c, email)
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
