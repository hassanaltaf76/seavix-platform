"""POST /api/register — public sign-up for ports agents, management
companies and surveyors.

Creates the Firebase user against the Auth emulator via its REST API (same
code path works in prod by dropping FIREBASE_AUTH_EMULATOR_HOST), then the
organization + owner membership in one DB transaction, then publishes
org.registered through the EventBus port.

The public slug is NOT collected here: the org gets a server-generated
placeholder slug (unique by construction) and the owner sets the real
public slug later in profile settings.
"""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from seavix_ports import domain_event, public_route

from .db import SessionLocal
from .orgs import _generate_slug
from .rbac import assign_roles, role_id_by_name, seed_default_roles
from .schemas import RegisterRequest, RegisterResponse

router = APIRouter()


async def _create_firebase_user(email: str, password: str) -> dict:
    emulator = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
    async with httpx.AsyncClient(base_url=f"http://{emulator}", timeout=10.0) as c:
        resp = await c.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signUp",
            params={"key": "demo-any-key"},
            json={"email": email, "password": password, "returnSecureToken": True},
        )
    body = resp.json()
    if resp.status_code != 200:
        msg = body.get("error", {}).get("message", "firebase signup failed")
        if msg in ("EMAIL_EXISTS", "EMAIL_TAKEN"):
            raise HTTPException(
                status_code=409,
                detail={"field": "email", "message": "email already registered"},
            )
        raise HTTPException(status_code=502, detail=f"identity provider error: {msg}")
    return body


def _grant_owner_tuple(firebase_uid: str, org_id: str) -> None:
    """Record org ownership in the authorization store via the AuthzClient port.

    Async helper fired-and-forgotten by the endpoint after commit.
    """
    import asyncio

    from seavix_ports import AuthzClient

    async def _write() -> None:
        authz = AuthzClient.from_env()
        await authz.write_tuples(
            [{"user": f"user:{firebase_uid}", "relation": "owner",
              "object": f"organization:{org_id}"}]
        )

    asyncio.get_running_loop().create_task(_write())


@router.post("/api/register", status_code=201)
@public_route
async def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    email = payload.email.lower()
    # 32a TODO (claiming — DELIBERATELY NOT BUILT): manual client creation
    # leaves an UNCLAIMED client org in the directory (type='client', no
    # membership/app_user — see POST /api/internal/orgs). When this
    # registration's email matches such an org's stored contact email, the
    # flow should CLAIM it instead of creating a duplicate org: attach the
    # new user as owner, seed roles, and flip the directory edge's meaning
    # from "unclaimed" to "registered client". Matching is by email exactly
    # as the dup-check below reads app_user.email; the claim transaction
    # replaces the INSERT block below. The directory edge
    # (work.client_directory) needs no change — client_org_id already
    # points at the org.
    async with SessionLocal() as session:
        if (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": email},
            )
        ).first():
            raise HTTPException(
                status_code=409,
                detail={"field": "email", "message": "email already registered"},
            )

        fb = await _create_firebase_user(email, payload.password)

        # Placeholder public slug — unique by construction (random suffix).
        # The owner picks the real slug in profile settings post-registration.
        slug = _generate_slug(payload.org_name)

        org_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO organization (id, name, type, slug, profile_status) "
                "VALUES (:id, :name, :type, :slug, 'draft')"
            ),
            {"id": org_id, "name": payload.org_name, "type": payload.org_type,
             "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO app_user (id, email, firebase_uid) "
                "VALUES (:id, :email, :uid)"
            ),
            {"id": user_id, "email": email, "uid": fb["localId"]},
        )
        await session.execute(
            text(
                "INSERT INTO membership (user_id, org_id, role) "
                "VALUES (:uid, :org, 'owner')"
            ),
            {"uid": user_id, "org": org_id},
        )
        # Default RBAC roles for the new org (D2) + owner binding.
        await seed_default_roles(session, org_id)
        owner_role_id = await role_id_by_name(session, org_id, "owner")
        await assign_roles(session, user_id, org_id, [owner_role_id])
        await session.commit()

    _grant_owner_tuple(fb["localId"], org_id)

    await request.app.state.event_bus.publish(
        "org.registered",
        domain_event(
            "org.registered",
            "organization",
            org_id,
            {"slug": slug, "org_name": payload.org_name,
             "org_type": payload.org_type},
        ),
    )
    return RegisterResponse(org_id=org_id, slug=slug)
