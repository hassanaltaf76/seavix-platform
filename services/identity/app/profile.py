"""Profile-management endpoints — intra-org RBAC via the identity DB
(effective permissions = union across the membership's roles).

- PUT  /api/orgs/me/profile        org.profile.edit
- POST /api/orgs/me/logo           org.logo.upload
- POST /api/orgs/me/profile-status org.profile.publish (+ verified email)
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator
from seavix_ports import AuthContext, require_auth
from sqlalchemy import text

from seavix_ports import LocalBlobStore, domain_event

from .config import blob_root
from .db import SessionLocal
from .permissions import Permission
from .schemas import SLUG_RE
from .security import require_permission

router = APIRouter()

# Root-level public URLs live at /<slug> on the web app, so every app path —
# current and planned — is a reserved slug. Concrete routes win at the router
# first; this list makes the collision impossible at the data layer too.
RESERVED_SLUGS = frozenset({
    "api", "static", "media", "healthz", "p", "org",
    "login", "register", "logout", "session",
    "forgot-password", "reset-password",
    "search", "app", "dashboard", "admin", "console",
    "settings", "account", "billing", "team", "teams", "users",
    "invite", "invites", "verify", "docs", "help", "support",
    "about", "contact", "pricing", "blog", "news",
    "terms", "privacy", "status", "download", "assets",
    "favicon.ico", "robots.txt", "sitemap.xml",
})

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
    slug: str | None = None  # optional: set/change the public page slug

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str | None) -> str | None:
        if v is not None and not (SLUG_RE.fullmatch(v) and 2 <= len(v) <= 64):
            raise ValueError(
                "slug must be 2-64 chars of lowercase letters, digits and dashes"
            )
        if v is not None and v in RESERVED_SLUGS:
            raise ValueError(f"slug {v!r} is a reserved word")
        return v


class StatusUpdate(BaseModel):
    status: str


async def _publish(request: Request, event_type: str, org_id: str, payload: dict) -> None:
    await request.app.state.event_bus.publish(
        event_type, domain_event(event_type, "organization", org_id, payload)
    )


@router.put("/api/orgs/me/profile")
async def update_profile(
    payload: ProfileUpdate, request: Request,
    ctx: AuthContext = Depends(require_auth),
) -> dict:
    membership = require_permission(ctx, Permission.ORG_PROFILE_EDIT.value)
    org_id = membership.org_id
    async with SessionLocal() as session:
        if payload.slug is not None:
            taken = (
                await session.execute(
                    text(
                        "SELECT 1 FROM organization WHERE slug = :slug "
                        "AND id <> :id"
                    ),
                    {"slug": payload.slug, "id": org_id},
                )
            ).first()
            if taken:
                raise HTTPException(
                    status_code=409,
                    detail={"field": "slug", "message": "slug already taken"},
                )
        await session.execute(
            text(
                "UPDATE organization SET about = :about, website = :website, "
                "phone = :phone, slug = COALESCE(:slug, slug) WHERE id = :id"
            ),
            {
                "about": payload.about,
                "website": payload.website,
                "phone": payload.phone,
                "slug": payload.slug,
                "id": org_id,
            },
        )
        await session.commit()
    return {"org_id": org_id, **payload.model_dump()}


@router.post("/api/orgs/me/logo")
async def upload_logo(
    request: Request, file: UploadFile,
    ctx: AuthContext = Depends(require_auth),
) -> dict:
    membership = require_permission(ctx, Permission.ORG_LOGO_UPLOAD.value)
    org_id = membership.org_id
    data = await file.read()
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="logo must be <= 2MB")
    mime = _sniff_mime(data)
    if mime is None:
        raise HTTPException(
            status_code=415, detail="logo must be a PNG, JPEG or WEBP image"
        )

    async with SessionLocal() as session:
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
async def set_profile_status(
    payload: StatusUpdate, request: Request,
    ctx: AuthContext = Depends(require_auth),
) -> dict:
    membership = require_permission(ctx, Permission.ORG_PROFILE_PUBLISH.value)
    org_id = membership.org_id
    if payload.status not in ("live", "draft"):
        raise HTTPException(status_code=400, detail="status must be live|draft")

    async with SessionLocal() as session:
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
        if payload.status == "live":
            verified = (
                await session.execute(
                    text("SELECT email_verified FROM app_user WHERE firebase_uid = :uid"),
                    {"uid": ctx.user.uid},
                )
            ).scalar_one()
            if not verified:
                raise HTTPException(
                    status_code=403,
                    detail="verify your email address before going live",
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
