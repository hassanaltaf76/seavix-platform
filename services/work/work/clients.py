"""Client directory (32a): tenant ↔ client edges over unclaimed client orgs.

A client IS an organization. Adding a client asks identity to create an
UNCLAIMED org (type='client', full 31t contact block, NO membership, NO
Firebase user — POST /api/internal/orgs, loopback-only) and records the
edge here. Work never reads identity's database: org data is fetched back
through identity's internal endpoints at read time.

Error surfacing (documented): if the identity call fails, POST /api/clients
returns 502 BEFORE any directory row is written — no edge without an org.
If the edge INSERT then violates UNIQUE(tenant_org_id, client_org_id), the
API returns 409; the unclaimed org identity just created is left in place
(it is inert — no membership, no sign-in — and the claiming TODO hook in
identity's register flow will reconcile by email later).
"""
from __future__ import annotations

import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .auth import identity_transport, require_permission, set_identity_transport
from .config import identity_base_url
from .db import SessionLocal

router = APIRouter()

# The identity transport seam lives in auth.py (one override for every
# work → identity call); re-exported here for the test wiring that
# 32a established at this module path.
__all__ = ["router", "set_identity_transport"]

RELATIONSHIP_TYPES = ("management", "surveyor_subcontract", "port_agent")

CLIENT_VIEW = "client.view"
CLIENT_CREATE = "client.create"


class ClientIn(BaseModel):
    """The work-side input for adding a client — passed through to
    identity's ClientOrgCreate (same field names, same validation rules;
    relationship_type is the directory edge, not an org field)."""

    name: str = Field(min_length=1, max_length=200)
    relationship_type: Literal[*RELATIONSHIP_TYPES]
    email: EmailStr
    contact_email: EmailStr | None = None
    # 32b (32a amendment): a quote addresses a person — "Dear Capt. Amin
    # Dar, Managing Director" (Pak Marine quote-doc field spec, §4).
    contact_person: str | None = Field(default=None, max_length=120)
    designation: str | None = Field(default=None, max_length=120)
    phone: str | None = None
    phone_cc: str | None = None
    whatsapp: str | None = None
    whatsapp_cc: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country_code: str | None = None


async def create_identity_org(payload: dict) -> dict:
    """Create the unclaimed client org in identity. On failure the caller
    must not write a directory edge: identity errors -> 502, while a 422
    (the 31t contact-field rules, owned and validated by identity) is
    propagated verbatim so the web app can render inline field errors."""
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=identity_transport()
    ) as c:
        resp = await c.post("/api/internal/orgs", json=payload)
    if resp.status_code == 422:
        raise HTTPException(status_code=422,
                            detail=resp.json().get("detail"))
    if resp.status_code != 201:
        raise HTTPException(status_code=502, detail="identity service error")
    return resp.json()


async def get_identity_org(org_id: str) -> dict:
    """Directory-field read of one org from identity (enriches edges)."""
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=identity_transport()
    ) as c:
        resp = await c.get(f"/api/internal/orgs/id/{org_id}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    return resp.json()


def _client_row(edge, org: dict) -> dict:
    return {
        "id": str(edge["id"]),
        "client_org_id": str(edge["client_org_id"]),
        "relationship_type": edge["relationship_type"],
        "created_at": edge["created_at"].isoformat(),
        "org": org,
    }


@router.post("/api/clients", status_code=201)
async def create_client(payload: ClientIn, request: Request) -> dict:
    member = await require_permission(request, CLIENT_CREATE)
    org = await create_identity_org(payload.model_dump())
    edge_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        try:
            await session.execute(
                text(
                    "INSERT INTO client_directory (id, tenant_org_id, "
                    "client_org_id, relationship_type, created_at) VALUES "
                    "(:id, :tenant, :client, :rel, now())"
                ),
                {"id": edge_id, "tenant": member["org_id"],
                 "client": org["org_id"], "rel": payload.relationship_type},
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="client already in the directory"
            ) from None
    return {"id": edge_id, "client_org_id": org["org_id"],
            "relationship_type": payload.relationship_type}


@router.get("/api/clients")
async def list_clients(request: Request) -> dict:
    member = await require_permission(request, CLIENT_VIEW)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, client_org_id, relationship_type, created_at "
                    "FROM client_directory WHERE tenant_org_id = :org "
                    "ORDER BY created_at"
                ),
                {"org": member["org_id"]},
            )
        ).mappings().all()
    clients = []
    for r in rows:
        org = await get_identity_org(str(r["client_org_id"]))
        clients.append(_client_row(r, org))
    return {"clients": clients}


@router.get("/api/clients/{client_id}")
async def get_client(request: Request, client_id: str) -> dict:
    member = await require_permission(request, CLIENT_VIEW)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, tenant_org_id, client_org_id, "
                    "relationship_type, created_at FROM client_directory "
                    "WHERE id = :id"
                ),
                {"id": client_id},
            )
        ).mappings().first()
    if row is None or str(row["tenant_org_id"]) != member["org_id"]:
        raise HTTPException(status_code=404, detail="client not found")
    org = await get_identity_org(str(row["client_org_id"]))
    return _client_row(row, org)
