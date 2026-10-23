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
from pydantic import BaseModel, EmailStr, field_validator
from seavix_ports import AuthContext, require_auth
import sqlalchemy as sa
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
    # 31m: public contact block. contact_email is the PUBLIC business
    # contact — it is validated and stored on its own column and is never
    # derived from (or checked against) the account email. address is
    # multi-line free text; whatsapp is international format with loose
    # validation (+, digits, spaces, dashes, parens; >= 7 digits).
    address: str | None = None
    contact_email: EmailStr | None = None
    # 31t: whatsapp keeps holding the NATIONAL number — the country
    # code moved to whatsapp_cc. Validation: digits/spaces/dashes,
    # 5-14 digits (parens/+ are no longer legal — the code has its own
    # field; the 31m punct-heavy formats are superseded, documented in
    # the 31t report).
    whatsapp: str | None = None
    # 31t: structured public address (migration 0013). All optional;
    # empty clears. When ANY structured field is set, the web render
    # shows structured and hides the legacy free-text `address` above.
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None  # free text, no format rule
    country_code: str | None = None  # ISO alpha-2
    phone_cc: str | None = None  # "+971"
    whatsapp_cc: str | None = None
    slug: str | None = None  # optional: set/change the public page slug
    # Ports of operation (task 31f): UN/LOCODE codes, cross-service
    # reference to registry.port — existence is validated by the web app
    # against the registry; identity validates the code FORMAT and owns
    # persistence. None = leave the org's ports untouched.
    ports: list[str] | None = None
    # 31q: published services + vessel types — codes against IDENTITY'S OWN
    # catalog tables (service / vessel_type, migration 0011). Existence is
    # validated in the endpoint against the catalog; the format here.
    # vessel_type codes are the shared vocabulary with registry's
    # vessel.taxonomy. None = leave untouched.
    services: list[str] | None = None
    vessel_types: list[str] | None = None

    @field_validator(
        "about", "website", "phone", "address", "contact_email",
        "address_line1", "address_line2", "city", "state", "postal_code",
        "country_code", "phone_cc", "whatsapp_cc", mode="before",
    )
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        # 31m: empty form fields clear the column instead of storing ""
        # (contact_email passes through here BEFORE EmailStr parsing)
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("phone", "whatsapp")
    @classmethod
    def national_number(cls, v: str | None) -> str | None:
        """31t: the national number (country code has its own column):
        digits, spaces and dashes only, 5-14 digits."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not re.fullmatch(r"[0-9\-\s]+", v):
            raise ValueError(
                "use only digits, spaces and dashes — the country code "
                "has its own field"
            )
        digits = len(re.sub(r"\D", "", v))
        if not 5 <= digits <= 14:
            raise ValueError(
                f"use 5-14 digits for the national number ({digits} given)"
            )
        return v

    @field_validator("country_code")
    @classmethod
    def iso_alpha2(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("country must be an ISO alpha-2 code (e.g. AE)")
        return v

    @field_validator("phone_cc", "whatsapp_cc")
    @classmethod
    def dial_code(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.fullmatch(r"\+\d{1,4}", v):
            raise ValueError(
                "country code must look like +971 (plus, 1-4 digits)"
            )
        return v

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

    @field_validator("ports")
    @classmethod
    def port_codes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            code = raw.strip()
            if not re.fullmatch(r"[A-Z]{5}", code):
                raise ValueError(
                    f"port code {raw!r} is not a UN/LOCODE "
                    "(exactly 5 uppercase letters/digits)"
                )
            if code not in out:
                out.append(code)
        return out

    @field_validator("services", "vessel_types")
    @classmethod
    def catalog_codes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            code = raw.strip()
            if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
                raise ValueError(
                    f"catalog code {raw!r} must be 1-64 lowercase "
                    "letters, digits and underscores"
                )
            if code not in out:
                out.append(code)
        return out


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
                "phone = :phone, address = :address, "
                "contact_email = :contact_email, whatsapp = :whatsapp, "
                "address_line1 = :address_line1, address_line2 = :address_line2, "
                "city = :city, state = :state, postal_code = :postal_code, "
                "country_code = :country_code, phone_cc = :phone_cc, "
                "whatsapp_cc = :whatsapp_cc, "
                "slug = COALESCE(:slug, slug) WHERE id = :id"
            ),
            {
                "about": payload.about,
                "website": payload.website,
                "phone": payload.phone,
                "address": payload.address,
                "contact_email": payload.contact_email,
                "whatsapp": payload.whatsapp,
                "address_line1": payload.address_line1,
                "address_line2": payload.address_line2,
                "city": payload.city,
                "state": payload.state,
                "postal_code": payload.postal_code,
                "country_code": payload.country_code,
                "phone_cc": payload.phone_cc,
                "whatsapp_cc": payload.whatsapp_cc,
                "slug": payload.slug,
                "id": org_id,
            },
        )
        if payload.ports is not None:
            # full replace: the Save button always submits the complete
            # chip list, so DELETE + INSERT is the whole semantics
            await session.execute(
                text("DELETE FROM org_port WHERE org_id = :id"), {"id": org_id}
            )
            if payload.ports:
                await session.execute(
                    text(
                        "INSERT INTO org_port (org_id, port_code) "
                        "VALUES (:id, :code) ON CONFLICT DO NOTHING"
                    ),
                    [{"id": org_id, "code": c} for c in payload.ports],
                )
        # 31q: services + vessel types ride the same full-replace Save.
        # Existence is checked against IDENTITY's own catalog BEFORE any
        # write (the org_* rows reference catalog codes, and identity owns
        # both sides here) — a 422 names the field exactly like a pydantic
        # error so the web app surfaces it inline next to the picker.
        for field, table, key, column, label in (
            ("services", "service", "service_code", "org_service", "service"),
            ("vessel_types", "vessel_type", "ship_type",
             "org_vessel_type", "vessel type"),
        ):
            codes = getattr(payload, field)
            if codes is None:
                continue
            unknown = list(codes)
            if codes:
                stmt = text(f"SELECT {key} FROM {table} "
                            f"WHERE {key} = ANY(:codes)").bindparams(
                    sa.bindparam("codes", type_=sa.ARRAY(sa.Text))
                )
                known = set(
                    (await session.execute(
                        stmt, {"codes": codes})).scalars().all()
                )
                unknown = [c for c in codes if c not in known]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=[{
                        "loc": [field],
                        "msg": f"unknown {label}: {unknown[0]}",
                    }],
                )
            await session.execute(
                text(f"DELETE FROM {column} WHERE org_id = :id"),
                {"id": org_id},
            )
            if codes:
                await session.execute(
                    text(
                        f"INSERT INTO {column} (org_id, {key}) "
                        "VALUES (:id, :code) ON CONFLICT DO NOTHING"
                    ),
                    [{"id": org_id, "code": c} for c in codes],
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
