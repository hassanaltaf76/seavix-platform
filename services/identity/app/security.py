"""AuthN/AuthZ plumbing for bearer-token endpoints.

- verify_token via the AuthProvider port (FirebaseAuthProvider; emulator in dev)
- org resolution via app_user + membership (single-org assumption, documented)
- role checks via the AuthzClient port (OpenFGA) — never raw HTTP here.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from seavix_ports import AuthzClient, AuthenticatedUser, FirebaseAuthProvider


def _ensure_firebase() -> None:
    import firebase_admin

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(
            options={"projectId": os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")}
        )


async def current_user(request: Request) -> AuthenticatedUser:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    _ensure_firebase()
    provider = FirebaseAuthProvider(
        project_id=os.environ.get("FIREBASE_PROJECT_ID", "seavix-dev")
    )
    try:
        return await provider.verify_token(header.removeprefix("Bearer ").strip())
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token") from None


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
