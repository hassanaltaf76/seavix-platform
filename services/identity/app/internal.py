"""Internal endpoints — platform services resolving bearer tokens to orgs.

Used by the work service (and future siblings) so they never touch the
identity database directly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from .db import SessionLocal
from .security import current_user, my_org_id

router = APIRouter()


@router.get("/api/internal/whoami")
async def whoami(request: Request) -> dict:
    user = await current_user(request)
    async with SessionLocal() as session:
        org_id = await my_org_id(session, user.uid)
        org = (
            await session.execute(
                text("SELECT slug, type FROM organization WHERE id = :id"),
                {"id": org_id},
            )
        ).mappings().one()
    return {"org_id": org_id, "slug": org["slug"], "type": org["type"],
            "uid": user.uid, "email": user.email}


@router.get("/api/internal/orgs/{slug}")
async def org_by_slug(slug: str) -> dict:
    """Service-to-service org lookup (public RFQ intake validation).

    No user bearer: the caller is anonymous (public RFQ submitter). Dev is
    localhost-only; production must protect this with service auth.
    """
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, slug, type, profile_status FROM organization "
                    "WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return {"org_id": str(row["id"]), "slug": row["slug"], "type": row["type"],
            "profile_status": row["profile_status"]}
