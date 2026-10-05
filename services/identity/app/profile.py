"""Profile-management endpoints (bearer token, owner role via AuthzClient port).

- PUT  /api/orgs/me/profile        about/website/phone
- POST /api/orgs/me/logo           multipart png|jpeg|webp, <=2MB
- POST /api/orgs/me/profile-status {status: live|draft}, survey_company only
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import text

from seavix_ports import LocalBlobStore, domain_event

from .config import blob_root
from .db import SessionLocal
from .security import current_user, my_org_id, require_owner

router = APIRouter()

MAX_LOGO_BYTES = 2 * 1024 * 1024
_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}


def _sniff_mime(data: bytes) -> str | None:
    for sig, mime in _MAGIC.items():
        if data.startswith(sig):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return cleaned or "logo"


class ProfileUpdate(BaseModel):
    about: str | None = None
    website: str | None = None
    phone: str | None = None


class StatusUpdate(BaseModel):
    status: str


async def _publish(request: Request, event_type: str, org_id: str, payload: dict) -> None:
    await request.app.state.event_bus.publish(
        event_type, domain_event(event_type, "organization", org_id, payload)
    )


@router.put("/api/orgs/me/profile")
async def update_profile(
    payload: ProfileUpdate, request: Request
) -> dict:
    user = await current_user(request)
    async with SessionLocal() as session:
        org_id = await my_org_id(session, user.uid)
        await require_owner(user.uid, org_id)
        await session.execute(
            text(
                "UPDATE organization SET about = :about, website = :website, "
                "phone = :phone WHERE id = :id"
            ),
            {
                "about": payload.about,
                "website": payload.website,
                "phone": payload.phone,
                "id": org_id,
            },
        )
        await session.commit()
    return {"org_id": org_id, **payload.model_dump()}


@router.post("/api/orgs/me/logo")
async def upload_logo(request: Request, file: UploadFile) -> dict:
    user = await current_user(request)
    data = await file.read()
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="logo must be <= 2MB")
    mime = _sniff_mime(data)
    if mime is None:
        raise HTTPException(
            status_code=415, detail="logo must be a PNG, JPEG or WEBP image"
        )

    async with SessionLocal() as session:
        org_id = await my_org_id(session, user.uid)
        await require_owner(user.uid, org_id)

        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
        base = re.sub(r"\.(png|jpe?g|webp)$", "", _safe_filename(file.filename or "logo"), flags=re.I)
        key = f"logos/{org_id}/{uuid.uuid4().hex[:8]}-{base}{ext}"
        store = LocalBlobStore(root=blob_root())
        await store.put(key, data)
        await session.execute(
            text("UPDATE organization SET logo_blob_key = :key WHERE id = :id"),
            {"key": key, "id": org_id},
        )
        await session.commit()

    await _publish(request, "org.logo_updated", org_id, {"logo_blob_key": key})
    return {"logo_blob_key": key, "mime": mime, "bytes": len(data)}


@router.post("/api/orgs/me/profile-status")
async def set_profile_status(payload: StatusUpdate, request: Request) -> dict:
    user = await current_user(request)
    if payload.status not in ("live", "draft"):
        raise HTTPException(status_code=400, detail="status must be live|draft")

    async with SessionLocal() as session:
        org_id = await my_org_id(session, user.uid)
        await require_owner(user.uid, org_id)
        org = (
            await session.execute(
                text("SELECT type, slug FROM organization WHERE id = :id"),
                {"id": org_id},
            )
        ).mappings().one()
        if org["type"] != "survey_company":
            raise HTTPException(
                status_code=400,
                detail="only survey_company organizations can go live",
            )
        await session.execute(
            text("UPDATE organization SET profile_status = :s WHERE id = :id"),
            {"s": payload.status, "id": org_id},
        )
        await session.commit()

    if payload.status == "live":
        await _publish(
            request, "org.profile_published", org_id, {"slug": org["slug"]}
        )
    return {"org_id": org_id, "slug": org["slug"], "profile_status": payload.status}
