"""System catalogs (31q) + custom entries and harvest (32d).

Identity-owned catalogs ONLY: service, vessel_type, and survey_type.
PORTS ARE EXCLUDED — UN/LOCODE is external reference data owned by the
registry, not our taxonomy; the ports picker never offers a create option
(contract-tested web-side).

32d mechanism: any signed-in member may CREATE a catalog entry from a
picker's "Create …" row. New rows carry origin='custom' +
created_by_org_id (NULL on the anonymous public-intake path) and appear in
EVERY org's picker search — system rows first, custom after, a "custom"
badge in the dropdown only (never on chips/public rendering). The catalog
row IS the harvest: later curation (promote/merge custom→system) is
stage-16 scope, documented and not built. Duplicate label (any origin,
case-insensitive) selects the existing entry instead of creating.

- GET  /api/catalog/<kind>/search?q=            -> {kind: [{code, label, origin}]}
- POST /api/catalog/<kind>       {label}        -> {code, label, origin} (201
                                                   created / 200 existing)
- GET  /api/public/catalog/survey-types/search  -> anonymous RFQ intake
- POST /api/public/catalog/survey-types         -> anonymous create (NULL
                                                   created_by_org_id)

Reference data — any signed-in member may query/extend it (the wall only,
no org permission); only the profile edit mode and the RFQ intake surface
it in the UI.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from seavix_ports import AuthContext, public_route, require_auth
from sqlalchemy import text

from .db import SessionLocal
from .schemas import CatalogEntry

router = APIRouter()

_LIMIT = 20
_LABEL_MAX = 60

# kind -> (table, code column). The survey_type catalog is additionally
# exposed anonymously for the public RFQ intake.
_CATALOGS = {
    "services": ("service", "service_code"),
    "vessel-types": ("vessel_type", "ship_type"),
    "survey-types": ("survey_type", "survey_type_code"),
}


class CatalogCreate(BaseModel):
    label: str = Field(min_length=1, max_length=_LABEL_MAX)


async def search_services(q: str = "") -> list[CatalogEntry]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT service_code AS code, label, origin FROM service "
                    "WHERE service_code ILIKE :frag OR label ILIKE :frag "
                    "ORDER BY (origin = 'system') DESC, sort_order, label "
                    "LIMIT :limit"
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
                    "SELECT ship_type AS code, label, origin FROM vessel_type "
                    "WHERE ship_type ILIKE :frag OR label ILIKE :frag "
                    "ORDER BY (origin = 'system') DESC, sort_order, label "
                    "LIMIT :limit"
                ),
                {"frag": f"%{q.strip()}%", "limit": _LIMIT},
            )
        ).mappings().all()
    return [CatalogEntry(**r) for r in rows]


async def search_survey_types(q: str = "") -> list[CatalogEntry]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT survey_type_code AS code, label, origin "
                    "FROM survey_type "
                    "WHERE survey_type_code ILIKE :frag OR label ILIKE :frag "
                    "ORDER BY (origin = 'system') DESC, sort_order, label "
                    "LIMIT :limit"
                ),
                {"frag": f"%{q.strip()}%", "limit": _LIMIT},
            )
        ).mappings().all()
    return [CatalogEntry(**r) for r in rows]


def _make_code(label: str) -> str:
    # codes are validated with [a-z0-9_]{1,64} wherever they are stored
    # (profile PUT, RFQ payloads) — underscores only, never dashes
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "custom"
    return f"{base[:48]}_{uuid.uuid4().hex[:6]}"


async def _create(kind: str, label: str, org_id: str | None) -> tuple[dict, int]:
    """Create a custom catalog entry, or select the existing one on a
    duplicate label (any origin, case-insensitive)."""
    table, code_col = _CATALOGS[kind]
    label = " ".join(label.split())  # trim + collapse inner whitespace
    if not label:
        raise HTTPException(status_code=422, detail="label is required")
    if len(label) > _LABEL_MAX:
        raise HTTPException(status_code=422, detail="label is too long")
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text(
                    f"SELECT {code_col} AS code, label, origin FROM {table} "
                    "WHERE lower(label) = lower(:label)"
                ),
                {"label": label},
            )
        ).mappings().first()
        if existing is not None:
            return dict(existing), 200
        code = _make_code(label)
        taken = (
            await session.execute(
                text(f"SELECT 1 FROM {table} WHERE {code_col} = :code"),
                {"code": code},
            )
        ).first()
        if taken:
            code = _make_code(label + " x")
        next_order = (
            await session.execute(
                text(f"SELECT COALESCE(MAX(sort_order), 0) + 1 FROM {table}")
            )
        ).scalar_one()
        await session.execute(
            text(
                f"INSERT INTO {table} ({code_col}, label, sort_order, origin, "
                "created_by_org_id, created_at) VALUES "
                "(:code, :label, :order, 'custom', :org, now())"
            ),
            {"code": code, "label": label, "order": next_order, "org": org_id},
        )
        await session.commit()
    return {"code": code, "label": label, "origin": "custom"}, 201


def _kind_or_404(kind: str) -> None:
    if kind not in _CATALOGS:
        raise HTTPException(status_code=404, detail="unknown catalog")


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


@router.get("/api/catalog/survey-types/search")
async def survey_types_search(
    q: str = "", ctx: AuthContext = Depends(require_auth),
) -> dict:
    return {
        "survey_types": [e.model_dump() for e in await search_survey_types(q)]
    }


@router.post("/api/catalog/{kind}")
async def catalog_create(
    kind: str, payload: CatalogCreate,
    ctx: AuthContext = Depends(require_auth),
) -> JSONResponse:
    """32d: create a custom entry (origin='custom', created_by_org_id = the
    caller's org — NULL when the caller has no membership). Duplicate label
    selects the existing entry (200, not 201)."""
    _kind_or_404(kind)
    org_id = ctx.memberships[0].org_id if ctx.memberships else None
    body, status = await _create(kind, payload.label, org_id)
    return JSONResponse(body, status_code=status)


@router.get("/api/public/catalog/survey-types/search")
@public_route
async def public_survey_types_search(q: str = "") -> dict:
    """Anonymous RFQ intake picker backend (the form is public)."""
    return {
        "survey_types": [e.model_dump() for e in await search_survey_types(q)]
    }


@router.post("/api/public/catalog/survey-types")
@public_route
async def public_survey_type_create(payload: CatalogCreate) -> JSONResponse:
    """Anonymous create from the public RFQ form: origin='custom' with NULL
    provenance (no signed-in org). Dev-stage openness is documented; prod
    needs rate limiting/spam review."""
    body, status = await _create("survey-types", payload.label, None)
    return JSONResponse(body, status_code=status)
