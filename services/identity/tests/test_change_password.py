"""32b FEATURE 3 (owner decision 24 Oct): signed-in change password, NO OTP.

Account → Change password for the SIGNED-IN user. The active opaque
session PLUS current-password verification is the reauthentication — the
flow must never touch the OTP ledger or the mail pipeline, and the normal
login OTP + forgot-password flows stay untouched.

Covered end to end at the identity API:
- wrong current password -> 422, ZERO mutation (Firebase still accepts
  the old password, sessions + trusted devices all live)
- mismatched confirm / new == current / policy violation -> 422, no mutation
- success: same Firebase UID, new password works + old fails; the CURRENT
  session survives; every OTHER session and every trusted device is revoked
- memberships / roles / organization rows unchanged; the OpenFGA owner
  tuple is still in place
- auth.password_changed emitted with visibility "both" and a payload with
  no secret values
- ZERO OTP/email: no new email_otp rows, no new outbox entries
- session-only: no session header (or only a bearer token) -> 401

Integration tests against the local emulators and the dev database, same
single-loop ASGI harness as test_authn.py.
"""
import os
import sys
import uuid

os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
os.environ.setdefault("FIREBASE_PROJECT_ID", "seavix-dev")
os.environ.setdefault("OPENFGA_HTTP_ADDR", "localhost:18080")
os.environ.setdefault("OPENFGA_STORE_ID", "01M45WT1F2VSVX6SKWGRTY17BB")

import httpx  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

OLD = "secret-123"
NEW = "brand-new-secret-9"


async def _register(c, email, password=OLD):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Change Password Org",
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


async def _login_with_otp(c, email, *, trust_device=False) -> dict:
    """Full two-factor login; returns the login/verify body (a fresh
    auth_session every call)."""
    start = await c.post(
        "/api/auth/login/start", json={"email": email, "password": OLD}
    )
    assert start.status_code == 200, start.text
    code = await _dev_code(c, email)
    body: dict = {"email": email, "code": code}
    if trust_device:
        body["trust_device"] = True
        body["device_user_agent"] = "pytest-change-password"
    verified = await c.post("/api/auth/login/verify", json=body)
    assert verified.status_code == 200, verified.text
    return verified.json()


async def _fb_sign_in(email: str, password: str) -> tuple[int, str | None]:
    """Direct Firebase check — did this password stop working?"""
    async with httpx.AsyncClient(base_url="http://localhost:9099") as c:
        resp = await c.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
            params={"key": "demo-any-key"},
            json={"email": email, "password": password,
                  "returnSecureToken": True},
        )
    local_id = resp.json().get("localId") if resp.status_code == 200 else None
    return resp.status_code, local_id


async def _user_row(email: str) -> dict:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        row = (
            await s.execute(
                sql("SELECT id, firebase_uid FROM app_user WHERE email = :e"),
                {"e": email},
            )
        ).mappings().first()
    return dict(row)


async def _session_rows(user_id: str) -> list[dict]:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                sql(
                    "SELECT id, revoked_at FROM auth_session "
                    "WHERE user_id = :u ORDER BY created_at"
                ),
                {"u": user_id},
            )
        ).mappings().all()
    return [{"id": str(r["id"]), "revoked_at": r["revoked_at"]} for r in rows]


async def _trusted_rows(user_id: str) -> list[dict]:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                sql(
                    "SELECT id, revoked_at FROM trusted_device "
                    "WHERE user_id = :u ORDER BY created_at"
                ),
                {"u": user_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _rbac_counts(user_id: str, org_id: str) -> dict:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        membership = (
            await s.execute(
                sql("SELECT count(*) FROM membership WHERE user_id = :u"),
                {"u": user_id},
            )
        ).scalar_one()
        roles = (
            await s.execute(
                sql("SELECT count(*) FROM membership_role WHERE user_id = :u"),
                {"u": user_id},
            )
        ).scalar_one()
        orgs = (
            await s.execute(sql("SELECT count(*) FROM organization"))
        ).scalar_one()
        org_row = (
            await s.execute(
                sql("SELECT name, type FROM organization WHERE id = :o"),
                {"o": org_id},
            )
        ).mappings().first()
    return {
        "membership": membership,
        "membership_role": roles,
        "organization": orgs,
        "org_row": dict(org_row),
    }


async def _otp_count(email: str) -> int:
    from sqlalchemy import text as sql

    async with SessionLocal() as s:
        return (
            await s.execute(
                sql("SELECT count(*) FROM email_otp WHERE email = :e"),
                {"e": email},
            )
        ).scalar_one()


def _outbox_len() -> int:
    from app.mailer import get_mailer

    return len(get_mailer().outbox)


@pytest.fixture()
def client():
    async def start():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    return start


async def _fully_signed_in(c, email):
    """Register + verify + two sessions + one trusted device; returns
    (sid_current, sid_other) with every precondition the change touches."""
    reg = await _register(c, email)
    await _verify_email(email)
    sid_current = (await _login_with_otp(c, email))["session_id"]
    sid_other = (await _login_with_otp(c, email))["session_id"]
    trusted = await _login_with_otp(c, email, trust_device=True)
    assert trusted.get("trusted_device")
    return reg, sid_current, sid_other


async def _assert_intact(c, email, user_id, sid_current, sid_other):
    """Post-condition of EVERY 422: the account is exactly as it was."""
    status, _ = await _fb_sign_in(email, OLD)
    assert status == 200  # Firebase still accepts the old password
    me = await c.get(
        "/api/auth/session", headers={"X-Seavix-Session": sid_current}
    )
    assert me.status_code == 200  # current session valid
    sessions = {r["id"]: r["revoked_at"] for r in await _session_rows(user_id)}
    assert sessions[sid_current] is None
    assert sessions[sid_other] is None  # no other session revoked either
    assert all(r["revoked_at"] is None for r in await _trusted_rows(user_id))


def test_wrong_current_password_422_zero_mutation(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"cp-{uuid.uuid4().hex[:8]}@example.com"
            reg, sid_current, sid_other = await _fully_signed_in(c, email)
            user = await _user_row(email)
            mail_before = _outbox_len()
            otp_before = await _otp_count(email)

            resp = await c.post(
                "/api/auth/change-password",
                headers={"X-Seavix-Session": sid_current},
                json={"current_password": "not-the-password",
                      "new_password": NEW, "confirm_password": NEW},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert detail["field"] == "current_password"

            await _assert_intact(c, email, str(user["id"]),
                                 sid_current, sid_other)
            assert _outbox_len() == mail_before  # ZERO email
            assert await _otp_count(email) == otp_before  # ZERO OTP rows

    arun(scenario())


def test_validation_failures_422_zero_mutation(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"cp-{uuid.uuid4().hex[:8]}@example.com"
            _, sid_current, sid_other = await _fully_signed_in(c, email)
            user = await _user_row(email)
            mail_before = _outbox_len()
            otp_before = await _otp_count(email)

            bad_payloads = [
                # mismatched confirm
                {"current_password": OLD, "new_password": NEW,
                 "confirm_password": "different-9"},
                # new == current
                {"current_password": OLD, "new_password": OLD,
                 "confirm_password": OLD},
                # policy violation (registration min-length is 6)
                {"current_password": OLD, "new_password": "abc12",
                 "confirm_password": "abc12"},
            ]
            for payload in bad_payloads:
                resp = await c.post(
                    "/api/auth/change-password",
                    headers={"X-Seavix-Session": sid_current},
                    json=payload,
                )
                assert resp.status_code == 422, (payload, resp.text)

            await _assert_intact(c, email, str(user["id"]),
                                 sid_current, sid_other)
            assert _outbox_len() == mail_before
            assert await _otp_count(email) == otp_before

    arun(scenario())


def test_change_password_success_rotates_and_revokes(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"cp-{uuid.uuid4().hex[:8]}@example.com"
            reg, sid_current, sid_other = await _fully_signed_in(c, email)
            org_id = reg["org_id"]
            user = await _user_row(email)
            user_id = str(user["id"])
            firebase_uid = user["firebase_uid"]
            before = await _rbac_counts(user_id, org_id)
            mail_before = _outbox_len()
            otp_before = await _otp_count(email)
            published_before = len(app.state.event_bus.published)

            resp = await c.post(
                "/api/auth/change-password",
                headers={"X-Seavix-Session": sid_current},
                json={"current_password": OLD, "new_password": NEW,
                      "confirm_password": NEW},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"changed": True}

            # same Firebase user: new password works, old one fails
            status_new, local_id = await _fb_sign_in(email, NEW)
            assert status_new == 200
            assert local_id == firebase_uid
            status_old, _ = await _fb_sign_in(email, OLD)
            assert status_old != 200

            # current session remains valid; the other one is dead
            me = await c.get(
                "/api/auth/session", headers={"X-Seavix-Session": sid_current}
            )
            assert me.status_code == 200
            other = await c.get(
                "/api/auth/session", headers={"X-Seavix-Session": sid_other}
            )
            assert other.status_code == 401
            sessions = {r["id"]: r["revoked_at"]
                        for r in await _session_rows(user_id)}
            assert sessions[sid_current] is None
            assert sessions[sid_other] is not None

            # every trusted device revoked
            devices = await _trusted_rows(user_id)
            assert devices and all(r["revoked_at"] is not None for r in devices)

            # memberships / roles / org rows untouched
            after = await _rbac_counts(user_id, org_id)
            assert after == before

            # the OpenFGA owner tuple survived (owner org registration)
            from seavix_ports import AuthzClient

            authz = AuthzClient.from_env()
            assert await authz.check(f"user:{firebase_uid}", "owner",
                                     f"organization:{org_id}") is True

            # event: visibility both, payload carries NO secret values
            new_events = app.state.event_bus.published[published_before:]
            mine = [e for t, e in new_events if t == "auth.password_changed"]
            assert len(mine) == 1
            event = mine[0]
            assert event["visibility"] == "both"
            assert event["payload"] == {"user_id": user_id}
            blob = str(event)
            for secret in (OLD, NEW):
                assert secret not in blob

            # ZERO OTP/email across the whole flow
            assert _outbox_len() == mail_before
            assert await _otp_count(email) == otp_before

    arun(scenario())


def test_session_only_bearer_or_anonymous_401(arun, client):
    async def scenario():
        async with await client() as c:
            email = f"cp-{uuid.uuid4().hex[:8]}@example.com"
            await _register(c, email)
            await _verify_email(email)

            payload = {"current_password": OLD, "new_password": NEW,
                       "confirm_password": NEW}

            # no credentials at all: the wall answers first
            anon = await c.post("/api/auth/change-password", json=payload)
            assert anon.status_code == 401

            # garbage session id
            garbage = await c.post(
                "/api/auth/change-password",
                headers={"X-Seavix-Session": str(uuid.uuid4())},
                json=payload,
            )
            assert garbage.status_code == 401

            # a VALID bearer token is NOT enough — session header only
            async with httpx.AsyncClient(
                base_url="http://localhost:9099"
            ) as fb:
                sign_in = await fb.post(
                    "/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
                    params={"key": "demo-any-key"},
                    json={"email": email, "password": OLD,
                          "returnSecureToken": True},
                )
                assert sign_in.status_code == 200, sign_in.text
                id_token = sign_in.json()["idToken"]
            bearer = await c.post(
                "/api/auth/change-password",
                headers={"Authorization": f"Bearer {id_token}"},
                json=payload,
            )
            assert bearer.status_code == 401

            # and again: ZERO mutation from every 401 path
            status, _ = await _fb_sign_in(email, OLD)
            assert status == 200

    arun(scenario())
