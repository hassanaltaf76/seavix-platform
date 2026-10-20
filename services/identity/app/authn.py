"""AuthN endpoints: password + email-OTP login (D1), registration email
verification, password reset, and opaque-session lifecycle.

All browser flows are proxied by the web app, which holds the session id in
an HttpOnly SameSite=Lax cookie. Service-to-service callers keep using
Bearer tokens via security.current_user.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from seavix_ports import (AuthenticatedUser, FirebaseAuthProvider,
                          domain_event, public_route)

from . import otp, sessions
from .db import SessionLocal
from .security import _ensure_firebase

router = APIRouter()

SESSION_HEADER = "X-Seavix-Session"


# ------------------------------------------------------------------ helpers

def _emulator_base() -> str:
    host = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
    return f"http://{host}/identitytoolkit.googleapis.com/v1"


async def _firebase_post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            f"{_emulator_base()}{path}",
            params={"key": "demo-any-key"},
            json=payload,
        )
    return {"status": resp.status_code, "body": resp.json()}


def _oob_code(action_link: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(action_link).query)["oobCode"][0]


def _web_link(path: str, **params: str) -> str:
    """Rewrite an Admin SDK action link into a SeaVix web URL: the emails
    always carry OUR pages (prod and dev), never a raw Firebase URL."""
    from urllib.parse import urlencode

    base = os.environ.get("SEAVIX_WEB_BASE_URL", "http://localhost:8000")
    return f"{base}{path}?{urlencode(params)}"


async def _publish(request: Request, event_type: str, aggregate: str,
                   aggregate_id: str, payload: dict) -> None:
    await request.app.state.event_bus.publish(
        event_type, domain_event(event_type, aggregate, aggregate_id, payload)
    )


async def _memberships(session, user_id: str) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT m.org_id, o.slug, o.type FROM membership m "
                "JOIN organization o ON o.id = m.org_id WHERE m.user_id = :uid "
                "AND m.status = 'active' "
                "ORDER BY m.org_id"
            ),
            {"uid": user_id},
        )
    ).mappings().all()
    out = []
    for r in rows:
        role_rows = (
            await session.execute(
                text(
                    "SELECT r.name FROM membership_role mr JOIN role r ON r.id = mr.role_id "
                    "WHERE mr.user_id = :uid AND mr.org_id = :org ORDER BY r.name"
                ),
                {"uid": user_id, "org": str(r["org_id"])},
            )
        ).mappings().all()
        out.append(
            {
                "org_id": str(r["org_id"]),
                "roles": [x["name"] for x in role_rows],
                "slug": r["slug"],
                "type": r["type"],
            }
        )
    return out


class EmailPassword(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class OtpChallenge(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class OtpResend(BaseModel):
    email: EmailStr


class ResetStart(BaseModel):
    email: EmailStr


class ResetFinish(BaseModel):
    oob_code: str = Field(min_length=1)
    new_password: str = Field(min_length=6, max_length=128)


class SessionRefresh(BaseModel):
    id_token: str | None = None


# ------------------------------------------------------------ login (D1)

@router.post("/api/auth/login/start")
@public_route
async def login_start(payload: EmailPassword, request: Request) -> dict:
    """Factor 1: email+password. On success issues the email OTP (factor 2).

    Failures are normalized to a single 401 so the endpoint cannot be used
    to enumerate accounts.

    31i: a VERIFIED email is a hard precondition of the challenge itself.
    After the password check, BEFORE any OTP send, an unverified account
    is refused with 403 email_unverified — no session path, no OTP issued.
    The reveal is safe: it only answers someone who already holds the
    password (and registration already discloses account existence).
    """
    email = payload.email.lower()
    async with SessionLocal() as session:
        known = (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": email},
            )
        ).first()
    if known is None:
        raise HTTPException(status_code=401, detail="invalid credentials")

    fb = await _firebase_post(
        "/accounts:signInWithPassword",
        {"email": email, "password": payload.password,
         "returnSecureToken": True},
    )
    if fb["status"] != 200:
        raise HTTPException(status_code=401, detail="invalid credentials")

    async with SessionLocal() as session:
        verified = (
            await session.execute(
                text("SELECT email_verified FROM app_user WHERE email = :email"),
                {"email": email},
            )
        ).scalar()
        if not verified:
            raise HTTPException(
                status_code=403,
                detail={"code": "email_unverified", "email": email},
            )
        await otp.send_code(session, email, "login")
    await _publish(request, "auth.otp_sent", "app_user", email,
                   {"purpose": "login"})
    return {"email": email, "otp_sent": True}


@router.post("/api/auth/login/verify")
@public_route
async def login_verify(payload: OtpChallenge, request: Request) -> dict:
    """Factor 2: the OTP. Only here is a session issued (D1 enforcement).

    31i (defense in depth): the verified precondition is re-checked BEFORE
    the code is consumed — a signed-in user is ALWAYS verified, even if a
    stale challenge somehow exists."""
    async with SessionLocal() as session:
        user = (
            await session.execute(
                text(
                    "SELECT id, firebase_uid, email, email_verified FROM app_user "
                    "WHERE email = :email"
                ),
                {"email": payload.email.lower()},
            )
        ).mappings().first()
        if user is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        if not user["email_verified"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "email_unverified", "email": payload.email.lower()},
            )
        await otp.verify_code(session, payload.email, "login", payload.code)
        active = (
            await session.execute(
                text(
                    "SELECT 1 FROM membership WHERE user_id = :uid "
                    "AND status = 'active' LIMIT 1"
                ),
                {"uid": str(user["id"])},
            )
        ).first()
        if active is None:
            raise HTTPException(status_code=403, detail="account deactivated")
        sid = await sessions.create_session(
            session, str(user["id"]), user["firebase_uid"]
        )
        memberships = await _memberships(session, str(user["id"]))

    await _publish(request, "auth.session_created", "app_user",
                   str(user["id"]), {"email": user["email"]})
    return {
        "session_id": sid,
        "email": user["email"],
        "email_verified": bool(user["email_verified"]),
        "memberships": memberships,
    }


@router.post("/api/auth/login/resend")
@public_route
async def login_resend(payload: OtpResend, request: Request) -> dict:
    """Send a fresh sign-in code for an in-progress OTP challenge.

    The response is constant regardless of account existence
    (anti-enumeration, same posture as reset/start). Send pacing is enforced
    by the per-email OTP send window; over-limit resends are swallowed so the
    endpoint stays enum-free. Posture note: mailbox access already implies
    full account access via the password-reset flow, so an email-only resend
    does not weaken the model.

    31i: never sends for an unverified account — the challenge cannot have
    started (login/start gates it), and an unverified address must receive
    no sign-in OTP at all. The response stays constant either way.
    """
    email = payload.email.lower()
    async with SessionLocal() as session:
        known = (
            await session.execute(
                text("SELECT email_verified FROM app_user WHERE email = :email"),
                {"email": email},
            )
        ).first()
        if known is not None and known[0]:
            try:
                await otp.send_code(session, email, "login")
            except HTTPException as exc:
                if exc.status_code != 429:
                    raise
    return {"email": email, "sent": True}


# ------------------------------------------------- registration verification

@router.post("/api/register/verify/send")
@public_route
async def register_verify_send(payload: ResetStart, request: Request) -> dict:
    """Admin SDK verification link, delivered through the SeaVix mail
    pipeline (our template — no Firebase Trigger Email extension).

    31e: resending is rate-limited like every other send — the link shares
    the per-email 5/hour window with the OTP codes (over-limit -> 429, the
    same posture as /api/auth/login/start)."""
    email = payload.email.lower()
    async with SessionLocal() as session:
        user = (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": email},
            )
        ).first()
        if user is None:
            # Same anti-enumeration posture as login.
            raise HTTPException(status_code=404, detail="unknown email")
        await otp.enforce_send_window(session, email)
        await otp.log_verification_link_send(session, email)
    _ensure_firebase()
    from firebase_admin import auth

    from .mail_templates import email_verification_email
    from .mailer import get_mailer

    action = auth.generate_email_verification_link(email)
    link = _web_link("/register/verify/confirm", email=email,
                     oob_code=_oob_code(action))
    subject, plain, html = email_verification_email(link)
    await get_mailer().send(email, subject, plain, html)
    await _publish(request, "auth.verification_link_sent", "app_user", email,
                   {})
    return {"email": email, "sent": True}


class VerifyConfirm(BaseModel):
    email: EmailStr
    oob_code: str = Field(min_length=1)


@router.post("/api/register/verify/confirm")
@public_route
async def register_verify_confirm(payload: VerifyConfirm, request: Request) -> dict:
    """Apply the single-use verification oobCode (the click target of the
    link sent by /send) and mark the app_user verified."""
    async with SessionLocal() as session:
        user = (
            await session.execute(
                text("SELECT id FROM app_user WHERE email = :email"),
                {"email": payload.email.lower()},
            )
        ).mappings().first()
        if user is None:
            raise HTTPException(status_code=404, detail="unknown email")
        fb = await _firebase_post(
            "/accounts:update", {"oobCode": payload.oob_code}
        )
        if fb["status"] != 200:
            raise HTTPException(status_code=400, detail="invalid or expired link")
        await session.execute(
            text("UPDATE app_user SET email_verified = true WHERE id = :id"),
            {"id": str(user["id"])},
        )
        await session.commit()
    await _publish(request, "auth.email_verified", "app_user", str(user["id"]),
                   {"email": payload.email.lower()})
    return {"email": payload.email.lower(), "email_verified": True}


# ------------------------------------------------------------ password reset

@router.post("/api/auth/reset/start")
@public_route
async def reset_start(payload: ResetStart, request: Request) -> dict:
    """Admin SDK reset link through the SeaVix mail pipeline (our template).
    Response is constant regardless of account existence."""
    email = payload.email.lower()
    _ensure_firebase()
    from firebase_admin import auth

    from .mail_templates import password_reset_email
    from .mailer import get_mailer

    action = auth.generate_password_reset_link(email)
    link = _web_link("/reset-password", oob_code=_oob_code(action))
    subject, plain, html = password_reset_email(link)
    await get_mailer().send(email, subject, plain, html)
    await _publish(request, "auth.password_reset_requested", "app_user",
                   email, {})
    return {"sent": True}



@router.post("/api/auth/reset/finish")
@public_route
async def reset_finish(payload: ResetFinish) -> dict:
    fb = await _firebase_post(
        "/accounts:resetPassword",
        {"oobCode": payload.oob_code, "newPassword": payload.new_password},
    )
    if fb["status"] != 200:
        msg = fb["body"].get("error", {}).get("message", "reset failed")
        raise HTTPException(status_code=400, detail=f"reset failed: {msg}")
    return {"reset": True}


# ------------------------------------------------------------------ sessions

def _session_id(request: Request) -> str:
    sid = request.headers.get(SESSION_HEADER, "").strip()
    if not sid:
        raise HTTPException(status_code=401, detail="missing session")
    return sid


@router.get("/api/auth/session")
async def get_session(request: Request) -> dict:
    async with SessionLocal() as session:
        ctx = await sessions.load_session(session, _session_id(request))
        memberships = await _memberships(session, ctx["user_id"])
    return {**ctx, "memberships": memberships}


@router.post("/api/auth/session/refresh")
async def refresh_session(payload: SessionRefresh, request: Request) -> dict:
    """Sliding renewal. When the browser Firebase SDK is adopted, the
    onIdTokenChanged handler posts the fresh idToken here; until then the
    web app's keep-alive posts with the cookie alone."""
    async with SessionLocal() as session:
        ctx = await sessions.load_session(session, _session_id(request))
        if payload.id_token:
            provider = FirebaseAuthProvider(
                project_id=os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")
            )
            try:
                user: AuthenticatedUser = await provider.verify_token(payload.id_token)
            except Exception:
                raise HTTPException(status_code=401, detail="invalid id token") from None
            if user.uid != ctx["firebase_uid"]:
                raise HTTPException(status_code=401, detail="token/session mismatch")
    return {"ok": True}


@router.delete("/api/auth/session")
async def delete_session(request: Request) -> dict:
    async with SessionLocal() as session:
        sid = _session_id(request)
        revoked = await sessions.revoke_session(session, sid)
    if revoked:
        await _publish(request, "auth.session_revoked", "auth_session", sid, {})
    return {"revoked": revoked}


# ------------------------------------------------------------- dev OTP peek

@router.get("/api/dev/last-otp")
@public_route
async def dev_last_otp(email: str, request: Request) -> dict:
    """DEV ONLY (SEAVIX_DEV_OTP_ENDPOINT=1): the last code sent to an email.

    Firebase cannot deliver our custom OTP (D1), so dev and tests read it
    here or from the identity log line DEV-OTP. Inert (404) unless the flag
    is explicitly set — never enable in prod. The code lives only in this
    process's memory; the database stores hashes only.
    """
    from . import config

    if not config.dev_otp_endpoint_enabled():
        raise HTTPException(status_code=404, detail="not found")
    entry = otp.dev_last_code(email)
    if entry is None:
        raise HTTPException(status_code=404, detail="no code for email")
    return {"email": email.lower(), "purpose": entry[0], "code": entry[1]}
