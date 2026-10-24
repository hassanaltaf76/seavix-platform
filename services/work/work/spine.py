"""RFQ → quote → job spine: endpoints + state machine.

State rules (enforced here, tested in tests/test_work.py):
- A sent quote is immutable: there is NO update path for line_items; a new
  revision supersedes prior sent quotes.
- Creating a quote: revision = max+1, any 'sent' quotes on the RFQ become
  'superseded', new quote is 'draft'.
- Send: draft → sent (quote.sent); RFQ awaiting_quote → quoted.
- Accept (single transaction): quote sent → accepted; sibling quotes
  (sent/draft) → superseded; RFQ → accepted; job row inserted. Emits
  quote.accepted and job.created.
- Decline: sent → declined.

32d: survey_types is a catalog-backed MULTI-select (identity owns the
survey_type catalog; selections are org-independent — any catalog code).
The legacy rfq.survey_type column keeps the FIRST selected code so
existing readers keep working; rfq_survey_type is authoritative.

32b: two-channel intake. channel records WHERE the RFQ arrived:
public_page (the marketing funnel) | manual_whatsapp | manual_email
(tenant raising a WhatsApp/email request "for record"). Fresh RFQs are
'awaiting_quote' (the old 'open' value — the display label is
"awaiting quote"). reference_no/vessel_name/port_name come from the
Pak Marine quote-doc field spec (master doc §4); client_id is the
tenant-directory EDGE the manual RFQ is linked to (no FK — soft
reference data). rfq.created carries channel + client provenance.
GET /api/rfqs/{id} is org-scoped: a caller whose org is not involved
(requesting/billing/target) gets a plain 404 — org-scoping IS the
authorization boundary (a client-role org never sees another client's
RFQ).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import bindparam, text, types

from seavix_ports import domain_event

from .auth import SESSION_HEADER, caller_member, caller_org, resolve_public_org
from .clients import RELATIONSHIP_TYPES, create_identity_org, get_identity_org
from .db import SessionLocal

router = APIRouter()

LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth", "dry-dock")
CHANNELS = ("public_page", "manual_whatsapp", "manual_email")

RFQ_COLS = (
    "id, requesting_org_id, billing_org_id, target_org_id, contact_email, "
    "vessel_imo, vessel_name, survey_type, location_type, port_name, "
    "preferred_date, reference_no, scope_notes, client_id, channel, "
    "source, status, created_at"
)


def _survey_type_codes(cls, v: list[str]) -> list[str]:
    out: list[str] = []
    for raw in v:
        code = raw.strip()
        if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            raise ValueError(
                f"survey type code {raw!r} must be 1-64 lowercase "
                "letters, digits and underscores"
            )
        if code not in out:
            out.append(code)
    return out


def _strip_or_none(cls, v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class RfqIn(BaseModel):
    target_org_id: str
    vessel_imo: str | None = None
    # 32b: the quoting surveyor addresses vessel + reference (Pak Marine
    # quote-doc field spec, master doc §4)
    vessel_name: str | None = Field(default=None, max_length=200)
    reference_no: str | None = Field(default=None, max_length=100)
    # 32b: free-text port/place alongside the location-type vocabulary
    port_name: str | None = Field(default=None, max_length=200)
    # 32d: catalog-backed multi-select (survey_type catalog codes —
    # system seeds or harvested custom entries; format-checked here,
    # existence lives in identity)
    survey_types: list[str] = Field(min_length=1, max_length=10)
    location_type: Literal[*LOCATION_TYPES]
    preferred_date: date | None = None
    scope_notes: str | None = None
    contact_email: EmailStr | None = None
    billing_org_id: str | None = None

    survey_type_codes = field_validator("survey_types")(_survey_type_codes)
    strip_fields = field_validator(
        "vessel_name", "reference_no", "port_name", "scope_notes"
    )(_strip_or_none)


class PublicRfqIn(BaseModel):
    org_slug: str = Field(min_length=2, max_length=64)
    contact_email: EmailStr | None = None  # 32b: optional — registered
        # funnel users are identified by their org, anonymous keeps email
    vessel_imo: str | None = None
    vessel_name: str | None = Field(default=None, max_length=200)
    reference_no: str | None = Field(default=None, max_length=100)
    port_name: str | None = Field(default=None, max_length=200)
    survey_types: list[str] = Field(min_length=1, max_length=10)
    location_type: Literal[*LOCATION_TYPES]
    preferred_date: date | None = None
    scope_notes: str | None = None

    survey_type_codes = field_validator("survey_types")(_survey_type_codes)
    strip_fields = field_validator(
        "vessel_name", "reference_no", "port_name", "scope_notes"
    )(_strip_or_none)


class NewClientIn(BaseModel):
    """Inline quick-create for the manual raise form: name + relationship
    + email — the full unclaimed-org contact block stays on the directory
    pages (32a)."""

    name: str = Field(min_length=1, max_length=200)
    relationship_type: Literal[*RELATIONSHIP_TYPES]
    email: EmailStr


class ManualRfqIn(BaseModel):
    """32b manual channel ("for record"): a tenant raises an RFQ that
    arrived over WhatsApp or email. The CLIENT is the requester; the
    tenant is the target (they will quote it — 32c). The client is linked
    to an existing directory edge OR quick-created inline (unclaimed org
    + edge in the same save — the identity call happens first, so an
    identity failure means NO rfq row)."""

    channel: Literal["manual_whatsapp", "manual_email"]
    client_id: str | None = None  # client_directory edge id
    new_client: NewClientIn | None = None
    vessel_name: str | None = Field(default=None, max_length=200)
    vessel_imo: str | None = None
    reference_no: str | None = Field(default=None, max_length=100)
    port_name: str | None = Field(default=None, max_length=200)
    survey_types: list[str] = Field(min_length=1, max_length=10)
    location_type: Literal[*LOCATION_TYPES]
    preferred_date: date | None = None
    scope_notes: str | None = None
    contact_email: EmailStr | None = None

    survey_type_codes = field_validator("survey_types")(_survey_type_codes)
    strip_fields = field_validator(
        "vessel_name", "reference_no", "port_name", "scope_notes"
    )(_strip_or_none)


class QuoteIn(BaseModel):
    line_items: list[dict] = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    valid_until: date | None = None


def _row(r) -> dict:
    d = dict(r)
    for k in ("requesting_org_id", "billing_org_id", "target_org_id",
              "client_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    if d.get("preferred_date") is not None:
        d["preferred_date"] = str(d["preferred_date"])
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
    # 32b: 'open' is the pre-32b name for 'awaiting_quote' — normalize so
    # legacy rows (and any reader still writing it) present the new
    # vocabulary.
    if d.get("status") == "open":
        d["status"] = "awaiting_quote"
    return d


def _quote_row(r) -> dict:
    d = _row(r)
    if isinstance(d.get("line_items"), str):
        d["line_items"] = json.loads(d["line_items"])
    if d.get("valid_until") is not None:
        d["valid_until"] = str(d["valid_until"])
    return d


async def _publish(request: Request, event_type: str, aggregate: str,
                   aggregate_id: str, payload: dict) -> None:
    await request.app.state.event_bus.publish(
        event_type, domain_event(event_type, aggregate, aggregate_id, payload)
    )


# ---------------------------------------------------------------- RFQs

INSERT_SURVEY_TYPES = (
    "INSERT INTO rfq_survey_type (rfq_id, survey_type) "
    "VALUES (:id, :code) ON CONFLICT DO NOTHING"
)


@router.post("/api/rfqs", status_code=201)
async def create_rfq(payload: RfqIn, request: Request) -> dict:
    caller = await caller_org(request)
    rfq_id = str(uuid.uuid4())
    billing = payload.billing_org_id or caller["org_id"]  # (b) default billing
    async with SessionLocal() as session:
        await session.execute(
            text(
                f"INSERT INTO rfq ({RFQ_COLS}) VALUES "
                "(:id, :req, :bill, :target, :contact, :imo, :vname, :stype, "
                " :ltype, :pname, :pdate, :ref, :scope, NULL, 'manual_email', "
                " 'internal', 'awaiting_quote', now())"
            ),
            {
                "id": rfq_id,
                "req": caller["org_id"],
                "bill": billing,
                "target": payload.target_org_id,
                "contact": payload.contact_email,
                "imo": payload.vessel_imo,
                "vname": payload.vessel_name,
                # 32d: legacy column keeps the FIRST code; the join below
                # is the authoritative multi-select storage
                "stype": payload.survey_types[0],
                "ltype": payload.location_type,
                "pname": payload.port_name,
                "pdate": payload.preferred_date,
                "ref": payload.reference_no,
                "scope": payload.scope_notes,
            },
        )
        await session.execute(
            text(INSERT_SURVEY_TYPES),
            [{"id": rfq_id, "code": c} for c in payload.survey_types],
        )
        await session.commit()
    await _publish(request, "rfq.created", "rfq", rfq_id,
                   {"target_org_id": payload.target_org_id,
                    "requesting_org_id": caller["org_id"],
                    "channel": "manual_email", "client_id": None,
                    "survey_types": payload.survey_types})
    return {"id": rfq_id, "status": "awaiting_quote"}


@router.post("/api/rfqs/manual", status_code=201)
async def create_manual_rfq(payload: ManualRfqIn, request: Request) -> dict:
    """32b manual intake ("for record"): the tenant records an RFQ that
    arrived over WhatsApp or email. The CLIENT org is the requester, the
    tenant the target (they will quote it — 32c). The client is an
    existing directory edge or an inline quick-create — the identity org
    + edge land FIRST, so an identity failure means no rfq row."""
    caller = await caller_org(request)
    if (payload.client_id is None) == (payload.new_client is None):
        raise HTTPException(
            status_code=422,
            detail="exactly one of client_id or new_client is required",
        )
    async with SessionLocal() as session:
        if payload.client_id is not None:
            edge = (
                await session.execute(
                    text(
                        "SELECT id, client_org_id FROM client_directory "
                        "WHERE id = :id AND tenant_org_id = :org"
                    ),
                    {"id": payload.client_id, "org": caller["org_id"]},
                )
            ).mappings().first()
            if edge is None:
                raise HTTPException(status_code=404, detail="client not found")
            client_org_id = str(edge["client_org_id"])
            edge_id = str(edge["id"])
        else:
            nc = payload.new_client
            org = await create_identity_org(
                {"name": nc.name, "type": "client", "email": str(nc.email),
                 "contact_email": str(nc.email)}
            )
            client_org_id = org["org_id"]
            edge_id = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO client_directory (id, tenant_org_id, "
                    "client_org_id, relationship_type, created_at) VALUES "
                    "(:id, :tenant, :client, :rel, now())"
                ),
                {"id": edge_id, "tenant": caller["org_id"],
                 "client": client_org_id, "rel": nc.relationship_type},
            )
        rfq_id = str(uuid.uuid4())
        await session.execute(
            text(
                f"INSERT INTO rfq ({RFQ_COLS}) VALUES "
                "(:id, :req, :bill, :target, :contact, :imo, :vname, :stype, "
                " :ltype, :pname, :pdate, :ref, :scope, :client, :channel, "
                " 'manual', 'awaiting_quote', now())"
            ),
            {
                "id": rfq_id,
                "req": client_org_id,
                "bill": client_org_id,
                "target": caller["org_id"],
                "contact": payload.contact_email,
                "imo": payload.vessel_imo,
                "vname": payload.vessel_name,
                "stype": payload.survey_types[0],
                "ltype": payload.location_type,
                "pname": payload.port_name,
                "pdate": payload.preferred_date,
                "ref": payload.reference_no,
                "scope": payload.scope_notes,
                "client": edge_id,
                "channel": payload.channel,
            },
        )
        await session.execute(
            text(INSERT_SURVEY_TYPES),
            [{"id": rfq_id, "code": c} for c in payload.survey_types],
        )
        await session.commit()
    await _publish(request, "rfq.created", "rfq", rfq_id,
                   {"target_org_id": caller["org_id"],
                    "requesting_org_id": client_org_id,
                    "channel": payload.channel, "client_id": edge_id,
                    "survey_types": payload.survey_types})
    return {"id": rfq_id, "status": "awaiting_quote"}


async def _optional_member(request: Request) -> dict | None:
    """32b: the public intake doubles as the registered funnel — when the
    caller's session/bearer resolves, the RFQ is attributed to their org.
    Anonymous (or garbage credentials) stays anonymous, like 32d."""
    if not request.headers.get(SESSION_HEADER) and \
            not request.headers.get("authorization", ""):
        return None
    try:
        return await caller_member(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def _org_names(ids) -> dict:
    """32b read-model enrichment: resolve the distinct org ids on RFQ rows
    to display names via identity (work never reads identity's DB — this
    is the same internal-org lookup the directory uses). Failures resolve
    to None and the UI falls back to a dash."""
    names: dict[str, str | None] = {}
    for oid in {str(i) for i in ids if i is not None}:
        try:
            names[oid] = (await get_identity_org(oid)).get("name")
        except Exception:
            names[oid] = None
    return names


@router.post("/api/public/rfqs", status_code=201)
async def create_public_rfq(payload: PublicRfqIn, request: Request) -> dict:
    org = await resolve_public_org(payload.org_slug)
    member = await _optional_member(request)
    requesting = member["org_id"] if member else None
    # 32b client provenance: a registered funnel submitter already in the
    # target's directory gets their edge recorded on the RFQ.
    client_id = None
    if member is not None:
        async with SessionLocal() as session:
            edge = (
                await session.execute(
                    text(
                        "SELECT id FROM client_directory "
                        "WHERE tenant_org_id = :tenant AND client_org_id = :client"
                    ),
                    {"tenant": org["org_id"], "client": member["org_id"]},
                )
            ).first()
            if edge is not None:
                client_id = str(edge[0])
    rfq_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        await session.execute(
            text(
                f"INSERT INTO rfq ({RFQ_COLS}) VALUES "
                "(:id, :req, NULL, :target, :contact, :imo, :vname, :stype, "
                " :ltype, :pname, :pdate, :ref, :scope, :client, 'public_page', "
                " 'public', 'awaiting_quote', now())"
            ),
            {
                "id": rfq_id,
                "req": requesting,
                "target": org["org_id"],
                "contact": payload.contact_email,
                "imo": payload.vessel_imo,
                "vname": payload.vessel_name,
                "stype": payload.survey_types[0],
                "ltype": payload.location_type,
                "pname": payload.port_name,
                "pdate": payload.preferred_date,
                "ref": payload.reference_no,
                "scope": payload.scope_notes,
                "client": client_id,
            },
        )
        await session.execute(
            text(INSERT_SURVEY_TYPES),
            [{"id": rfq_id, "code": c} for c in payload.survey_types],
        )
        await session.commit()
    await _publish(request, "rfq.created", "rfq", rfq_id,
                   {"target_slug": payload.org_slug,
                    "target_org_id": org["org_id"],
                    "requesting_org_id": requesting,
                    "channel": "public_page", "client_id": client_id,
                    "survey_types": payload.survey_types})
    return {"id": rfq_id, "status": "awaiting_quote"}


@router.get("/api/rfqs")
async def list_rfqs(
    request: Request, status: str = Query("", max_length=20)
) -> dict:
    caller = await caller_org(request)
    clause = "(requesting_org_id = :org OR target_org_id = :org)"
    params: dict = {"org": caller["org_id"]}
    if status:
        clause += " AND status = :status"
        params["status"] = status
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE {clause} "
                     "ORDER BY created_at"),
                params,
            )
        ).mappings().all()
        # 32b: the multi-select survey types ride each row (the list page
        # renders one chip per selection)
        by_id: dict[str, list[str]] = {str(r["id"]): [] for r in rows}
        if rows:
            stmt = text(
                "SELECT rfq_id, survey_type FROM rfq_survey_type "
                "WHERE rfq_id = ANY(:ids) ORDER BY survey_type"
            ).bindparams(bindparam("ids", type_=types.ARRAY(types.Uuid)))
            for r in (
                await session.execute(stmt, {"ids": [r["id"] for r in rows]})
            ).mappings().all():
                by_id[str(r["rfq_id"])].append(r["survey_type"])
    out = []
    for r in rows:
        d = _row(r)
        d["survey_types"] = by_id.get(str(r["id"]), [])
        out.append(d)
    ids = [i for r in rows for i in
           (r["requesting_org_id"], r["target_org_id"], r["billing_org_id"])]
    return {"rfqs": out, "org_names": await _org_names(ids)}


@router.get("/api/rfqs/{rfq_id}")
async def get_rfq(request: Request, rfq_id: str) -> dict:
    caller = await caller_org(request)  # authenticated read
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE id = :id"), {"id": rfq_id}
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="rfq not found")
        # 32b: org-scoping IS the authorization boundary — a caller whose
        # org is not involved gets a plain 404 (never a 403, task-23
        # policy). This is what keeps a client-role org from ever seeing
        # another client's RFQ.
        involved = {row["requesting_org_id"], row["billing_org_id"],
                    row["target_org_id"]}
        if caller["org_id"] not in {str(o) for o in involved if o is not None}:
            raise HTTPException(status_code=404, detail="rfq not found")
        quotes = (
            await session.execute(
                text(
                    "SELECT id, revision, line_items, currency, valid_until, "
                    "status, created_at FROM quote WHERE rfq_id = :id "
                    "ORDER BY revision"
                ),
                {"id": rfq_id},
            )
        ).mappings().all()
        # 32d: authoritative multi-select survey types (legacy column
        # carries the first code)
        survey_types = (
            await session.execute(
                text(
                    "SELECT survey_type FROM rfq_survey_type WHERE rfq_id = :id "
                    "ORDER BY survey_type"
                ),
                {"id": rfq_id},
            )
        ).scalars().all()
    return {**_row(row), "quotes": [_quote_row(q) for q in quotes],
            "survey_types": list(survey_types),
            "org_names": await _org_names(
                (row["requesting_org_id"], row["target_org_id"],
                 row["billing_org_id"]))}


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict:
    caller = await caller_org(request)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, rfq_id, quote_id, type, survey_type, vessel_imo, "
                    "requesting_org_id, billing_org_id, owner_org_id, status, "
                    "created_at FROM job WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    d = _row(row)
    d["id"] = str(d["id"])
    d["rfq_id"] = str(d["rfq_id"])
    d["quote_id"] = str(d["quote_id"])
    involved = {d["requesting_org_id"], d["billing_org_id"], d["owner_org_id"]}
    if caller["org_id"] not in involved:
        raise HTTPException(status_code=403, detail="org not involved in job")
    return d


# ---------------------------------------------------------------- quotes

async def _load_quote(session, quote_id: str) -> dict:
    row = (
        await session.execute(
            text(
                "SELECT q.id, q.rfq_id, q.revision, q.line_items, q.currency, "
                "q.valid_until, q.status, q.created_at, r.target_org_id, "
                "r.requesting_org_id, r.survey_type, r.vessel_imo, "
                "r.billing_org_id, r.status AS rfq_status "
                "FROM quote q JOIN rfq r ON r.id = q.rfq_id WHERE q.id = :id"
            ),
            {"id": quote_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="quote not found")
    return dict(row)


@router.post("/api/rfqs/{rfq_id}/quotes", status_code=201)
async def create_quote(rfq_id: str, payload: QuoteIn, request: Request) -> dict:
    caller = await caller_org(request)
    quote_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        rfq = (
            await session.execute(
                text("SELECT target_org_id, status FROM rfq WHERE id = :id"),
                {"id": rfq_id},
            )
        ).mappings().first()
        if rfq is None:
            raise HTTPException(status_code=404, detail="rfq not found")
        if str(rfq["target_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the target org quotes")
        next_rev = (
            await session.execute(
                text("SELECT COALESCE(MAX(revision), 0) + 1 FROM quote WHERE rfq_id = :id"),
                {"id": rfq_id},
            )
        ).scalar_one()
        # supersede prior sent quotes; drafts stay (author may still send)
        await session.execute(
            text("UPDATE quote SET status = 'superseded' "
                 "WHERE rfq_id = :id AND status = 'sent'"),
            {"id": rfq_id},
        )
        await session.execute(
            text(
                "INSERT INTO quote (id, rfq_id, revision, line_items, currency, "
                "valid_until, status, created_at) VALUES "
                "(:id, :rfq, :rev, CAST(:items AS jsonb), :cur, :valid, 'draft', now())"
            ),
            {
                "id": quote_id,
                "rfq": rfq_id,
                "rev": next_rev,
                "items": json.dumps(payload.line_items),
                "cur": payload.currency.upper(),
                "valid": payload.valid_until,
            },
        )
        await session.commit()
    return {"id": quote_id, "rfq_id": rfq_id, "revision": next_rev, "status": "draft"}


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(quote_id: str, request: Request) -> dict:
    caller = await caller_org(request)
    async with SessionLocal() as session:
        q = await _load_quote(session, quote_id)
        if str(q["target_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the target org sends")
        if q["status"] != "draft":
            raise HTTPException(status_code=409, detail=f"cannot send from {q['status']}")
        await session.execute(
            text("UPDATE quote SET status = 'sent' WHERE id = :id"), {"id": quote_id}
        )
        await session.execute(
            text("UPDATE rfq SET status = 'quoted' WHERE id = :id AND "
                 "status IN ('open', 'awaiting_quote')"),
            {"id": str(q["rfq_id"])},
        )
        await session.commit()
    await _publish(request, "quote.sent", "quote", quote_id,
                   {"rfq_id": str(q["rfq_id"]), "revision": q["revision"]})
    return {"id": quote_id, "status": "sent"}


@router.post("/api/quotes/{quote_id}/accept", status_code=201)
async def accept_quote(quote_id: str, request: Request) -> dict:
    caller = await caller_org(request)
    async with SessionLocal() as session:
        q = await _load_quote(session, quote_id)
        if q["requesting_org_id"] is None:
            raise HTTPException(
                status_code=409,
                detail="anonymous public RFQs cannot be accepted yet "
                       "(requester conversion is a later slice)",
            )
        if str(q["requesting_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the requesting org accepts")
        if q["status"] != "sent":
            raise HTTPException(status_code=409, detail=f"cannot accept from {q['status']}")

        # --- single transaction: accept + supersede siblings + rfq + job ---
        job_id = str(uuid.uuid4())
        await session.execute(
            text("UPDATE quote SET status = 'accepted' WHERE id = :id"), {"id": quote_id}
        )
        await session.execute(
            text(
                "UPDATE quote SET status = 'superseded' WHERE rfq_id = :rfq "
                "AND id != :id AND status IN ('sent', 'draft')"
            ),
            {"rfq": str(q["rfq_id"]), "id": quote_id},
        )
        await session.execute(
            text("UPDATE rfq SET status = 'accepted' WHERE id = :id"),
            {"id": str(q["rfq_id"])},
        )
        await session.execute(
            text(
                "INSERT INTO job (id, rfq_id, quote_id, type, survey_type, "
                "vessel_imo, requesting_org_id, billing_org_id, owner_org_id, "
                "status, created_at) VALUES "
                "(:id, :rfq, :quote, 'survey', :stype, :imo, :req, :bill, "
                " :owner, 'scheduled', now())"
            ),
            {
                "id": job_id,
                "rfq": str(q["rfq_id"]),
                "quote": quote_id,
                "stype": q["survey_type"],
                "imo": q["vessel_imo"],
                "req": q["requesting_org_id"],
                "bill": q["billing_org_id"] or q["requesting_org_id"],
                "owner": q["target_org_id"],
            },
        )
        await session.commit()
    await _publish(request, "quote.accepted", "quote", quote_id,
                   {"rfq_id": str(q["rfq_id"]), "job_id": job_id})
    await _publish(request, "job.created", "job", job_id,
                   {"rfq_id": str(q["rfq_id"]), "quote_id": quote_id,
                    "owner_org_id": str(q["target_org_id"])})
    return {"quote_id": quote_id, "status": "accepted", "job_id": job_id}


@router.post("/api/quotes/{quote_id}/decline")
async def decline_quote(quote_id: str, request: Request) -> dict:
    caller = await caller_org(request)
    async with SessionLocal() as session:
        q = await _load_quote(session, quote_id)
        if q["requesting_org_id"] is None or \
                str(q["requesting_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the requesting org declines")
        if q["status"] != "sent":
            raise HTTPException(status_code=409, detail=f"cannot decline from {q['status']}")
        await session.execute(
            text("UPDATE quote SET status = 'declined' WHERE id = :id"), {"id": quote_id}
        )
        await session.commit()
    return {"id": quote_id, "status": "declined"}
