"""Public, unauthenticated read model for organizations.

Public-safe JSON only: never email, never memberships, never firebase_uids.
logo_url is a relative /media/ path served by the web app (dev: streams from
var/blobs). profile_status is exposed so the web app can render the
not-available page for non-live profiles.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from seavix_ports import public_route
from sqlalchemy import text

from .db import SessionLocal
from .schemas import PublicOrgResponse

router = APIRouter()


@router.get("/api/orgs/{slug}")
@public_route
async def get_public_org(slug: str) -> PublicOrgResponse:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, name, type, about, website, phone, address, "
                    "contact_email, whatsapp, logo_blob_key, profile_status "
                    "FROM organization WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="organization not found")
        ports = (
            await session.execute(
                text(
                    "SELECT port_code FROM org_port WHERE org_id = :id "
                    "ORDER BY port_code"
                ),
                {"id": row["id"]},
            )
        ).scalars().all()
        # 31q: services + vessel types resolved to [{code, label}] rows in
        # catalog sort order — identity owns these catalogs, so the join
        # happens right here (contrast 31f ports: registry-owned, the web
        # app resolves names there)
        services = (
            await session.execute(
                text(
                    "SELECT s.service_code AS code, s.label FROM "
                    "org_service os JOIN service s "
                    "ON s.service_code = os.service_code "
                    "WHERE os.org_id = :id ORDER BY s.sort_order"
                ),
                {"id": row["id"]},
            )
        ).mappings().all()
        vessel_types = (
            await session.execute(
                text(
                    "SELECT v.ship_type AS code, v.label FROM "
                    "org_vessel_type ov JOIN vessel_type v "
                    "ON v.ship_type = ov.ship_type "
                    "WHERE ov.org_id = :id ORDER BY v.sort_order"
                ),
                {"id": row["id"]},
            )
        ).mappings().all()
    return PublicOrgResponse(
        slug=slug,
        name=row["name"],
        type=row["type"],
        about=row["about"],
        website=row["website"],
        phone=row["phone"],
        address=row["address"],
        contact_email=row["contact_email"],
        whatsapp=row["whatsapp"],
        logo_url=f"/media/{row['logo_blob_key']}" if row["logo_blob_key"] else None,
        profile_status=row["profile_status"],
        ports=list(ports),
        services=[dict(r) for r in services],
        vessel_types=[dict(r) for r in vessel_types],
    )
