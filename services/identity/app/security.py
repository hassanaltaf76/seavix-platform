"""AuthN/AuthZ plumbing for bearer-token endpoints.

- verify_token via the AuthProvider port (FirebaseAuthProvider; emulator in dev)
- org resolution via app_user + membership (single-org assumption, documented)
- role checks via the AuthzClient port (OpenFGA) — never raw HTTP here.
- IdentityAuthLoader: resolves Bearer/session credentials into the shared
  AuthContext for the deny-by-default middleware (task 21). Roles and
  permissions are wired for real in task 22; the shapes are final.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from seavix_ports.authn import AuthContext, MembershipContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from seavix_ports import AuthzClient, AuthenticatedUser, FirebaseAuthProvider

from . import sessions
from .db import SessionLocal

SESSION_HEADER = "X-Seavix-Session"
SCOPE_KEY = "seavix.auth"


def _ensure_firebase() -> None:
    import firebase_admin

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(
            options={"projectId": os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")}
        )


async def current_user(request: Request) -> AuthenticatedUser:
    """Resolve the caller from a Bearer token OR the web session header.

    Bearer: Firebase ID token via the AuthProvider port (machines, tests).
    X-Seavix-Session: opaque session id issued only after the email-OTP
    challenge (D1) — the web app's HttpOnly cookie maps to this header.
    When the deny-by-default middleware already resolved the request, its
    AuthContext is reused (no second Firebase round-trip).
    """
    ctx = request.scope.get(SCOPE_KEY)
    if ctx is not None:
        return ctx.user

    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        _ensure_firebase()
        provider = FirebaseAuthProvider(
            project_id=os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")
        )
        try:
            return await provider.verify_token(header.removeprefix("Bearer ").strip())
        except Exception:
            raise HTTPException(status_code=401, detail="invalid token") from None

    sid = request.headers.get(SESSION_HEADER, "").strip()
    if sid:
        async with SessionLocal() as session:
            ctx = await sessions.load_session(session, sid)
        return AuthenticatedUser(uid=ctx["firebase_uid"], email=ctx["email"])

    raise HTTPException(status_code=401, detail="missing credentials")


async def my_org_id(session: AsyncSession, uid: str) -> str:
    """Org of the user's (first) membership. Scaffold assumes one org per user."""
    row = (
        await session.execute(
            text(
                "SELECT m.org_id FROM membership m "
                "JOIN app_user u ON u.id = m.user_id "
                "WHERE u.firebase_uid = :uid ORDER BY m.org_id LIMIT 1"
            ),
            {"uid": uid},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=403, detail="no organization membership")
    return str(row[0])


async def require_owner(uid: str, org_id: str) -> None:
    authz = AuthzClient.from_env()
    allowed = await authz.check(f"user:{uid}", "owner", f"organization:{org_id}")
    if not allowed:
        raise HTTPException(status_code=403, detail="owner role required")


# ------------------------------------------------------ AuthContext (task 21)

class IdentityAuthLoader:
    """Credential -> AuthContext against the identity database.

    Used by the deny-by-default middleware. Fails closed: any problem
    resolves to None, which the middleware turns into 401.
    """

    async def load_from_bearer(self, token: str) -> AuthContext | None:
        _ensure_firebase()
        provider = FirebaseAuthProvider(
            project_id=os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")
        )
        try:
            user = await provider.verify_token(token)
        except Exception:
            return None
        return await self.build(user)

    async def load_from_session(self, session_id: str) -> AuthContext | None:
        try:
            async with SessionLocal() as session:
                ctx = await sessions.load_session(session, session_id)
        except Exception:
            return None
        user = AuthenticatedUser(uid=ctx["firebase_uid"], email=ctx["email"])
        built = await self.build(user)
        if built is None:
            return None
        return AuthContext(
            user=built.user,
            email_verified=ctx["email_verified"],
            memberships=built.memberships,
        )

    async def build(self, user: AuthenticatedUser) -> AuthContext | None:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, email_verified FROM app_user WHERE firebase_uid = :uid"
                    ),
                    {"uid": user.uid},
                )
            ).mappings().first()
            if row is None:
                return None
            memberships = (
                await session.execute(
                    text(
                        "SELECT m.org_id, m.role, o.slug, o.type "
                        "FROM membership m JOIN organization o ON o.id = m.org_id "
                        "WHERE m.user_id = :uid ORDER BY m.org_id"
                    ),
                    {"uid": str(row["id"])},
                )
            ).mappings().all()
        return AuthContext(
            user=AuthenticatedUser(uid=user.uid, email=user.email),
            email_verified=bool(row["email_verified"]),
            memberships=tuple(
                # Task 22 replaces the legacy membership.role text with
                # role-table resolution; permissions stay a union (deny
                # never overrides allow at this layer).
                MembershipContext(
                    org_id=str(m["org_id"]),
                    slug=m["slug"],
                    org_type=m["type"],
                    roles=(m["role"],),
                    permissions=frozenset(),
                )
                for m in memberships
            ),
        )
