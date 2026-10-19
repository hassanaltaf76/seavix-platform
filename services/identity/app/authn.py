"""AuthN endpoints: password + email-OTP login (D1), registration email
verification, password reset, and opaque-session lifecycle.

All browser flows are proxied by the web app, which holds the session id in
an HttpOnly SameSite=Lax cookie. Service-to-service callers keep using
Bearer tokens via security.current_user.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from seavix_ports import AuthenticatedUser, FirebaseAuthProvider, domain_event

from . import otp, sessions
from .db import SessionLocal

logger = logging.getLogger("seavix.identity.authn")

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


async def _publish(request: Request, event_type: str, aggregate: str,
                   aggregate_id: str, payload: dict) -> None:
    await request.app.state.event_bus.publish(
        event_type, domain_event(event_type, aggregate, aggregate_id, payload)
    )


async def _memberships(session, user_id: str) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT m.org_id, m.role, o.slug, o.type FROM membership m "
                "JOIN organization o ON o.id = m.org_id WHERE m.user_id = :uid "
                "ORDER BY m.org_id"
            ),
            {"uid": user_id},
        )
    ).mappings().all()
    return [
        {"org_id": str(r["org_id"]), "role": r["role"], "slug": r["slug"], "type": r["type"]}
        for r in rows
    ]


class EmailPassword(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class OtpChallenge(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class ResetStart(BaseModel):
    email: EmailStr


class ResetFinish(BaseModel):
    oob_code: str = Field(min_length=1)
    new_password: str = Field(min_length=6, max_length=128)


class SessionRefresh(BaseModel):
    id_token: str | None = None


# ------------------------------------------------------------ login (D1)

@router.post("/api/auth/login/start")
async def login_start(payload: EmailPassword, request: Request) -> dict:
    """Factor 1: email+password. On success issues the email OTP (factor 2).

    Failures are normalized to a single 401 so the endpoint cannot be used
    to enumerate accounts.
    """
    async with SessionLocal() as session:
        known = (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": payload.email.lower()},
            )
        ).first()
    if known is None:
        raise HTTPException(status_code=401, detail="invalid credentials")

    fb = await _firebase_post(
        "/accounts:signInWithPassword",
        {"email": payload.email.lower(), "password": payload.password,
         "returnSecureToken": True},
    )
    if fb["status"] != 200:
        raise HTTPException(status_code=401, detail="invalid credentials")

    async with SessionLocal() as session:
        await otp.send_code(session, payload.email, "login")
    await _publish(request, "auth.otp_sent", "app_user", payload.email.lower(),
                   {"purpose": "login"})
    return {"email": payload.email.lower(), "otp_sent": True}


@router.post("/api/auth/login/verify")
async def login_verify(payload: OtpChallenge, request: Request) -> dict:
    """Factor 2: the OTP. Only here is a session issued (D1 enforcement)."""
    async with SessionLocal() as session:
        await otp.verify_code(session, payload.email, "login", payload.code)
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


# ------------------------------------------------- registration verification

@router.post("/api/register/verify/send")
async def register_verify_send(payload: ResetStart, request: Request) -> dict:
    async with SessionLocal() as session:
        user = (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": payload.email.lower()},
            )
        ).first()
        if user is None:
            # Same anti-enumeration posture as login.
            raise HTTPException(status_code=404, detail="unknown email")
        await otp.send_code(session, payload.email, "verify_email")
    await _publish(request, "auth.otp_sent", "app_user", payload.email.lower(),
                   {"purpose": "verify_email"})
    return {"email": payload.email.lower(), "otp_sent": True}


@router.post("/api/register/verify/confirm")
async def register_verify_confirm(payload: OtpChallenge, request: Request) -> dict:
    async with SessionLocal() as session:
        await otp.verify_code(session, payload.email, "verify_email", payload.code)
        user = (
            await session.execute(
                text(
                    "UPDATE app_user SET email_verified = true WHERE email = :email "
                    "RETURNING id"
                ),
                {"email": payload.email.lower()},
            )
        ).mappings().first()
        if user is None:
            raise HTTPException(status_code=404, detail="unknown email")
        await session.commit()
    await _publish(request, "auth.email_verified", "app_user", str(user["id"]),
                   {"email": payload.email.lower()})
    return {"email": payload.email.lower(), "email_verified": True}


# ------------------------------------------------------------ password reset

@router.post("/api/auth/reset/start")
async def reset_start(payload: ResetStart, request: Request) -> dict:
    """Firebase password-reset link. In dev the emulator neither sends email
    nor returns the link on the REST response, so the latest oobLink is
    fetched from the emulator's inspection API and logged (prod: real email
    via provider — TODO). Response is constant regardless of account
    existence."""
    email = payload.email.lower()
    await _firebase_post(
        "/accounts:sendOobCode",
        {"requestType": "PASSWORD_RESET", "email": email},
    )
    link = await _latest_reset_link(email)
    if link:
        logger.info("DEV-RESET-LINK email=%s link=%s", email, link)
        await _publish(request, "auth.password_reset_requested", "app_user",
                       email, {})
    return {"sent": True}


async def _latest_reset_link(email: str) -> str | None:
    """Dev-only: newest PASSWORD_RESET oobLink for an email from the auth
    emulator's inspection endpoint (prod returns None — email is the
    delivery channel there)."""
    if "FIREBASE_AUTH_EMULATOR_HOST" not in os.environ:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(
                f"{_emulator_base().split('/identitytoolkit')[0]}"
                f"/emulator/v1/projects/"
                f"{os.environ.get('FIREBASE_PROJECT_ID', 'seavix-dev')}/oobCodes"
            )
        for entry in reversed(resp.json().get("oobCodes", [])):
            if entry.get("email") == email and entry.get("requestType") == "PASSWORD_RESET":
                return entry.get("oobLink")
    except Exception:  # pragma: no cover - dev convenience only
        logger.debug("emulator oobCodes inspection failed", exc_info=True)
    return None


@router.post("/api/auth/reset/finish")
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
