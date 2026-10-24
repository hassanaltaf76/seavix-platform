"""Internal endpoints — platform services resolving bearer tokens to orgs.

Used by the work service (and future siblings) so they never touch the
identity database directly.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from seavix_ports import SCOPE_KEY, context_to_dict, domain_event, public_route
from sqlalchemy import text

from .db import SessionLocal
from .orgs import _generate_slug
from .profile import ClientOrgCreate
from .security import IdentityAuthLoader, current_user, my_org_id

router = APIRouter()

# 32a service-to-service guard: the mutation/read pair below is reachable
# only from loopback (or in-process ASGI calls, which carry no client).
# Same documented posture as the rest of /api/internal/* (see the note on
# org_by_slug): dev is localhost-only, production must protect this with
# service auth (mTLS or a shared secret).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _require_loopback(request: Request) -> None:
    client = request.client
    if client is not None and client.host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/api/internal/auth-context")
async def auth_context(request: Request) -> dict:
    """The caller's full AuthContext (memberships, roles, effective
    permissions) as wire-format JSON. Sibling services (web) feed their
    deny-by-default middleware from this endpoint so no service reads
    another's database."""
    ctx = request.scope.get(SCOPE_KEY)
    if ctx is None:
        built = await IdentityAuthLoader().build(await current_user(request))
        if built is None:
            raise HTTPException(status_code=401, detail="authentication required")
        ctx = built
    return context_to_dict(ctx)


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
@public_route
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


@router.post("/api/internal/orgs", status_code=201)
@public_route
async def create_client_org(payload: ClientOrgCreate, request: Request) -> dict:
    """32a: create an UNCLAIMED client organization (type='client').

    Called by the work service while adding a client to a tenant's
    directory. The org carries the full 31t contact block but has NO
    membership, NO app_user and NO Firebase account — it is claimed later
    when a self-registration matches on email (TODO hook in register.py;
    claiming is deliberately NOT built in 32a). Roles are NOT seeded either:
    seeding happens at registration/claim time. Publishes org.created
    (lifecycle parity with every other org creation) and client.created
    (the directory-domain event).
    """
    _require_loopback(request)
    org_id = str(uuid.uuid4())
    slug = _generate_slug(payload.name)
    contact_email = str(payload.contact_email or payload.email).lower()
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO organization (id, name, type, slug, contact_email, "
                "contact_person, designation, "
                "phone, phone_cc, whatsapp, whatsapp_cc, address_line1, "
                "address_line2, city, state, postal_code, country_code, "
                "created_at) VALUES (:id, :name, 'client', :slug, :cemail, "
                ":cperson, :designation, "
                ":phone, :phone_cc, :whatsapp, :whatsapp_cc, :a1, :a2, :city, "
                ":state, :postal, :country, now())"
            ),
            {
                "id": org_id, "name": payload.name, "slug": slug,
                "cemail": contact_email,
                "cperson": payload.contact_person,
                "designation": payload.designation,
                "phone": payload.phone,
                "phone_cc": payload.phone_cc, "whatsapp": payload.whatsapp,
                "whatsapp_cc": payload.whatsapp_cc,
                "a1": payload.address_line1, "a2": payload.address_line2,
                "city": payload.city, "state": payload.state,
                "postal": payload.postal_code, "country": payload.country_code,
            },
        )
        await session.commit()

    bus = request.app.state.event_bus
    await bus.publish(
        "org.created",
        domain_event("org.created", "organization", org_id,
                     {"name": payload.name, "type": "client"}),
    )
    await bus.publish(
        "client.created",
        domain_event("client.created", "organization", org_id,
                     {"name": payload.name,
                      "email": str(payload.email).lower()}),
    )
    return {"org_id": org_id, "slug": slug, "type": "client",
            "contact_email": contact_email,
            "contact_person": payload.contact_person,
            "designation": payload.designation}


@router.get("/api/internal/orgs/id/{org_id}")
@public_route
async def org_by_id(org_id: str, request: Request) -> dict:
    """32a: service-to-service read of one org's directory fields (the work
    service enriches client-directory rows with these). Loopback-only, same
    guard as create_client_org. 32c: the commercial defaults ride along —
    the work service prefills new quotes with quote_prefix/default_terms/
    payment_details (NULL until the org saves commercial settings)."""
    _require_loopback(request)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, name, type, slug, contact_email, "
                    "contact_person, designation, "
                    "phone, phone_cc, whatsapp, whatsapp_cc, address_line1, "
                    "address_line2, city, state, postal_code, country_code, "
                    "quote_prefix, default_terms, payment_details "
                    "FROM organization WHERE id = :id"
                ),
                {"id": org_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="organization not found")
    d = dict(row)
    d["id"] = str(d["id"])
    return d
