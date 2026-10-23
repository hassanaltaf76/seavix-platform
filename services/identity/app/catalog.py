"""System catalogs (task 31q): the survey-service catalog and the
vessel-type taxonomy behind the profile editor's chips comboboxes.

- GET /api/catalog/services/search?q=     -> {"services": [{code, label}]}
- GET /api/catalog/vessel-types/search?q= -> {"vessel_types": [{code, label}]}

Reference data — any signed-in member may query it (the wall only, no
org permission); only the profile edit mode surfaces it in the UI. The
vessel-type codes are the SAME vocabulary registry's vessel.taxonomy
uses; identity carries its own catalog copy (0011) so reads never need a
cross-service round trip. Catalog admin-editing is a later slice.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from seavix_ports import AuthContext, require_auth
from sqlalchemy import text

from .db import SessionLocal
from .schemas import CatalogEntry

router = APIRouter()

_LIMIT = 20


async def search_services(q: str = "") -> list[CatalogEntry]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT service_code AS code, label FROM service "
                    "WHERE service_code ILIKE :frag OR label ILIKE :frag "
                    "ORDER BY sort_order LIMIT :limit"
                ),
                {"frag": f"%{q.strip()}%", "limit": _LIMIT},
            )
        ).mappings().all()
    return [CatalogEntry(**r) for r in rows]


async def search_vessel_types(q: str = "") -> list[CatalogEntry]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT ship_type AS code, label FROM vessel_type "
                    "WHERE ship_type ILIKE :frag OR label ILIKE :frag "
                    "ORDER BY sort_order LIMIT :limit"
                ),
                {"frag": f"%{q.strip()}%", "limit": _LIMIT},
            )
        ).mappings().all()
    return [CatalogEntry(**r) for r in rows]


@router.get("/api/catalog/services/search")
async def services_search(
    q: str = "", ctx: AuthContext = Depends(require_auth),
) -> dict:
    return {"services": [e.model_dump() for e in await search_services(q)]}


@router.get("/api/catalog/vessel-types/search")
async def vessel_types_search(
    q: str = "", ctx: AuthContext = Depends(require_auth),
) -> dict:
    return {
        "vessel_types": [e.model_dump() for e in await search_vessel_types(q)]
    }
