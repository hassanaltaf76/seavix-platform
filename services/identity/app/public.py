"""Public, unauthenticated read model for organizations.

Public-safe JSON only: never email, never memberships, never firebase_uids.
logo_url is a relative /media/ path served by the web app (dev: streams from
var/blobs). profile_status is exposed so the web app can render the
not-available page for non-live profiles.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from .db import SessionLocal
from .schemas import PublicOrgResponse

router = APIRouter()


@router.get("/api/orgs/{slug}")
async def get_public_org(slug: str) -> PublicOrgResponse:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT name, about, website, phone, logo_blob_key, profile_status "
                    "FROM organization WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return PublicOrgResponse(
        slug=slug,
        name=row["name"],
        about=row["about"],
        website=row["website"],
        phone=row["phone"],
        logo_url=f"/media/{row['logo_blob_key']}" if row["logo_blob_key"] else None,
        profile_status=row["profile_status"],
    )
