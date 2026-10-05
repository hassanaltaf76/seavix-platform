"""POST /api/register — public sign-up for survey companies and clients.

Creates the Firebase user against the Auth emulator via its REST API (same
code path works in prod by dropping FIREBASE_AUTH_EMULATOR_HOST), then the
organization + owner membership in one DB transaction, then publishes
org.registered through the EventBus port.
"""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from seavix_ports import domain_event

from .db import SessionLocal
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


@router.post("/api/register", status_code=201)
async def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    async with SessionLocal() as session:
        if (
            await session.execute(
                text("SELECT 1 FROM organization WHERE slug = :slug"),
                {"slug": payload.slug},
            )
        ).first():
            raise HTTPException(
                status_code=409, detail={"field": "slug", "message": "slug already taken"}
            )
        if (
            await session.execute(
                text("SELECT 1 FROM app_user WHERE email = :email"),
                {"email": payload.email},
            )
        ).first():
            raise HTTPException(
                status_code=409,
                detail={"field": "email", "message": "email already registered"},
            )

        fb = await _create_firebase_user(payload.email, payload.password)

        org_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO organization (id, name, type, slug, profile_status) "
                "VALUES (:id, :name, :type, :slug, 'draft')"
            ),
            {
                "id": org_id,
                "name": payload.org_name,
                "type": payload.account_kind,  # org.type carries the account kind
                "slug": payload.slug,
            },
        )
        await session.execute(
            text(
                "INSERT INTO app_user (id, email, firebase_uid) "
                "VALUES (:id, :email, :uid)"
            ),
            {"id": user_id, "email": payload.email, "uid": fb["localId"]},
        )
        await session.execute(
            text(
                "INSERT INTO membership (user_id, org_id, role) "
                "VALUES (:uid, :org, 'owner')"
            ),
            {"uid": user_id, "org": org_id},
        )
        await session.commit()

    await request.app.state.event_bus.publish(
        "org.registered",
        domain_event(
            "org.registered",
            "organization",
            org_id,
            {
                "slug": payload.slug,
                "org_name": payload.org_name,
                "account_kind": payload.account_kind,
            },
        ),
    )
    return RegisterResponse(org_id=org_id, slug=payload.slug)
