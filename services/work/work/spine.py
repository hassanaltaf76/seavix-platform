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

32c: the quote becomes a printable document (Pak Marine quote-doc
spec). tenant_org_id scopes every quote read/write (org-scoping IS
authz, task-23 — permission misses are a plain 404). quote_no is a
per-tenant/year human number ("PREFIX-NN-YEAR", allocated atomically
from quote_sequence). RFQ echo fields + a client-org SNAPSHOT land on
the row at creation (directory edits never rewrite a document); terms
default from the org profile (or DEFAULT_TERMS) and bank_details from
the org's payment_details, both stored per-quote. A draft is editable
(PUT); the ONLY way out is the human approve+issue gate
(POST /{id}/approve-issue), which stamps approved_by/issued_by and
flips the RFQ to 'quoted' in one transaction — a plain draft save
never issues. POST /{id}/revision copies a quote into a fresh draft
revision (prior 'sent' superseded; RFQ untouched).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import bindparam, text, types

from seavix_ports import domain_event

from .auth import (SESSION_HEADER, caller_member, caller_org,
                   identity_transport, resolve_public_org)
from .clients import RELATIONSHIP_TYPES, create_identity_org, get_identity_org
from .config import identity_base_url
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

# The nine Pak Marine standard terms — the default when the org profile
# carries no default_terms of its own (master doc §4).
DEFAULT_TERMS = [
    "Our offer is valid for 30 days from the date of this quotation.",
    "Condition report will be submitted within 36 hours of survey completion.",
    "Pre-purchase report will be submitted within 72 hours of survey completion.",
    "Payment is due within 07 days of invoice date.",
    "Travel expenses will be charged at actuals plus traveling days.",
    "Anchorage surcharge applies as applicable.",
    "Gate pass arrangements are on client's account.",
    "50% advance payment is required before mobilization.",
    "Bank charges are on client's account.",
]


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


def _currency_code(cls, v: str | None) -> str | None:
    """32c: ISO-4217-ish — strip, uppercase, exactly three letters."""
    if v is None:
        return None
    v = v.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", v):
        raise ValueError("currency must be a 3-letter code")
    return v


def _terms_list(cls, v: list[str] | None) -> list[str] | None:
    """32c: strip, drop empties, cap 50 entries at 500 chars each."""
    if v is None:
        return None
    out = [t.strip()[:500] for t in v if isinstance(t, str) and t.strip()]
    return out[:50]


def _bank_details(cls, v: dict | None) -> dict | None:
    """32c: string keys, str|number|null values, at most 20 keys."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError("bank_details must be an object")
    if len(v) > 20:
        raise ValueError("bank_details allows at most 20 keys")
    for k, val in v.items():
        if not isinstance(k, str):
            raise ValueError("bank_details keys must be strings")
        if val is not None and not isinstance(val, (str, int, float)):
            raise ValueError(
                "bank_details values must be strings, numbers or null")
    return v


class QuoteIn(BaseModel):
    """32c: the quote document body — every field optional (a bare POST
    drafts a document with org defaults). line_items are validated in the
    endpoint via _validate_line_items so they round-trip VERBATIM."""

    quote_date: date | None = None
    valid_until: date | None = None
    currency: str | None = None
    line_items: list[dict] | None = None
    terms: list[str] | None = None
    bank_details: dict | None = None

    currency_code = field_validator("currency")(_currency_code)
    terms_list = field_validator("terms")(_terms_list)
    bank = field_validator("bank_details")(_bank_details)


class QuoteUpdate(BaseModel):
    """32c: draft edits — same fields as QuoteIn, all optional. None means
    "not provided": this endpoint never clears a field by omission."""

    quote_date: date | None = None
    valid_until: date | None = None
    currency: str | None = None
    line_items: list[dict] | None = None
    terms: list[str] | None = None
    bank_details: dict | None = None

    currency_code = field_validator("currency")(_currency_code)
    terms_list = field_validator("terms")(_terms_list)
    bank = field_validator("bank_details")(_bank_details)


class ApproveIssueIn(BaseModel):
    """32c: the human gate — the client confirms before the document goes
    out. Anything but {"confirm": true} is a 422."""

    confirm: bool


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


# ---------------------------------------------------------------- quote
# document helpers (32c)

_QUOTE_UUID_COLS = ("id", "rfq_id", "tenant_org_id", "prepared_by",
                    "approved_by", "issued_by")
_QUOTE_JSON_COLS = ("line_items", "survey_types", "client_address", "terms",
                    "bank_details")
_QUOTE_STR_COLS = ("quote_date", "valid_until")


def _quote_out(r) -> dict:
    """The full quote row as an API dict: JSONB columns parsed, ids and
    dates stringified, plus the server-side total. Used by every quote
    response (create/list/detail/update/issue/revision AND the embedded
    quotes on GET /api/rfqs/{id})."""
    d = dict(r)
    for k in _QUOTE_JSON_COLS:
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    for k in _QUOTE_UUID_COLS:
        if d.get(k) is not None:
            d[k] = str(d[k])
    for k in _QUOTE_STR_COLS:
        if d.get(k) is not None:
            d[k] = str(d[k])
    for k in ("created_at", "updated_at", "approved_at", "issued_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    d["total"] = str(_quote_total(d.get("line_items")))
    return d


def _validate_line_items(items) -> list:
    """32c: shape-check the line items and return them VERBATIM (same
    dicts, extra keys untouched) — the stored document must echo exactly
    what the user entered. Empty list is allowed on a draft."""
    if not isinstance(items, list):
        raise HTTPException(status_code=422,
                            detail="line_items must be a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}] must be an object")
        desc = item.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].description must be a non-empty string")
        amount = item.get("amount")
        if isinstance(amount, bool) or amount is None:
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].amount must be a number")
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].amount is not a valid number")
        if not value.is_finite():
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].amount is not a valid number")
        if value.as_tuple().exponent < -2:
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].amount allows at most 2 decimal places")
        if value < 0:
            raise HTTPException(
                status_code=422,
                detail=f"line_items[{i}].amount must not be negative")
    return items


def _quote_total(line_items) -> Decimal:
    """Server-side sum of the (validated) amounts, quantized to 2dp."""
    total = Decimal("0")
    for item in line_items or []:
        total += Decimal(str(item["amount"]))
    return total.quantize(Decimal("0.01"))


def _json_val(v):
    """Raw SELECTs hand jsonb back as a string (asyncpg has no column
    type for text() statements) — parse before re-serializing."""
    return json.loads(v) if isinstance(v, str) else v


NEXT_QUOTE_SEQ = (
    "INSERT INTO quote_sequence (tenant_org_id, year, last_seq) "
    "VALUES (:tenant, :year, 1) "
    "ON CONFLICT (tenant_org_id, year) "
    "DO UPDATE SET last_seq = quote_sequence.last_seq + 1 "
    "RETURNING last_seq"
)


async def _next_quote_no(session, tenant_org_id, prefix: str,
                         year: int) -> str:
    """Atomic per-(tenant, year) number: the upsert serializes concurrent
    creates on the sequence row, so two writers never get one number."""
    seq = (
        await session.execute(
            text(NEXT_QUOTE_SEQ), {"tenant": tenant_org_id, "year": year}
        )
    ).scalar_one()
    return f"{prefix}-{seq:02d}-{year}"


def _normalize_prefix(raw: str) -> str:
    """Uppercase, keep [A-Z0-9], collapse the rest into '-', strip and
    truncate to 12 chars."""
    return re.sub(r"[^A-Z0-9]+", "-", raw.upper()).strip("-")[:12]


async def _org_commercial(tenant_org_id: str) -> dict:
    """The tenant org's commercial profile: default_terms, payment_details
    and the quote-number prefix. Best-effort — identity failures resolve
    to None (callers fall back to DEFAULT_TERMS / no bank block)."""
    out: dict = {"default_terms": None, "payment_details": None,
                 "quote_prefix": None}
    try:
        org = await get_identity_org(tenant_org_id)
    except Exception:
        return out
    if not isinstance(org, dict):
        return out
    out["default_terms"] = org.get("default_terms")
    out["payment_details"] = org.get("payment_details")
    prefix = org.get("quote_prefix")
    if isinstance(prefix, str) and prefix.strip():
        out["quote_prefix"] = prefix.strip()
        return out
    slug = org.get("slug")
    if isinstance(slug, str):
        normalized = _normalize_prefix(slug)
        if len(normalized) >= 2:
            out["quote_prefix"] = normalized
            return out
    # degenerate: "Q" + org id hex, truncated to the 12-char budget
    out["quote_prefix"] = ("Q" + str(tenant_org_id).replace("-", "")
                           .upper())[:12]
    return out


def _snapshot_from_org(org: dict) -> dict:
    """Client-org fields -> quote snapshot columns (all best-effort)."""
    org = org or {}
    address = {k: org.get(k) for k in
               ("address_line1", "address_line2", "city", "state",
                "postal_code", "country_code")}
    if not any(v is not None for v in address.values()):
        address = None
    phone = None
    if org.get("phone") is not None:
        cc = (org.get("phone_cc") or "").strip()
        phone = f"{cc}{org['phone']}" if cc else str(org["phone"])
    return {
        "client_name": org.get("name"),
        "client_contact_person": org.get("contact_person"),
        "client_designation": org.get("designation"),
        "client_address": address,
        "client_country": org.get("country_code"),
        "client_phone": phone,
        "client_email": org.get("contact_email"),
    }


async def _client_snapshot(session, rfq) -> dict:
    """The quote's client block, SNAPSHOT at creation: directory edge ->
    client org fields, or the requesting org (public intake), or just the
    RFQ's contact email. Best-effort throughout — an identity failure must
    never 500 the quote create; the snapshot fields simply stay None."""
    snap = {"client_name": None, "client_contact_person": None,
            "client_designation": None, "client_address": None,
            "client_country": None, "client_phone": None,
            "client_email": None}
    try:
        if rfq.get("client_id"):
            edge = (
                await session.execute(
                    text("SELECT client_org_id FROM client_directory "
                         "WHERE id = :id"),
                    {"id": str(rfq["client_id"])},
                )
            ).mappings().first()
            if edge is not None:
                return {**snap, **_snapshot_from_org(
                    await get_identity_org(str(edge["client_org_id"])))}
        elif rfq.get("requesting_org_id"):
            return {**snap, **_snapshot_from_org(
                await get_identity_org(str(rfq["requesting_org_id"])))}
    except Exception:
        return snap
    if not rfq.get("client_id") and not rfq.get("requesting_org_id"):
        snap["client_email"] = rfq.get("contact_email")
    return snap


async def require_permission(request: Request, permission: str) -> dict:
    """404-over-403 (task-23 policy). Spine-local so the caller_member
    seam lives in THIS module — tests patch one name (spine.caller_member)."""
    member = await caller_member(request)
    if permission not in member.get("permissions", []):
        raise HTTPException(status_code=404, detail="not found")
    return member


async def _load_quote_row(session, quote_id: str):
    """SELECT * FROM quote WHERE id = :id -> mapping or None."""
    return (
        await session.execute(
            text("SELECT * FROM quote WHERE id = :id"), {"id": quote_id}
        )
    ).mappings().first()


async def _publish(request: Request, event_type: str, aggregate: str,
                   aggregate_id: str, payload: dict,
                   visibility: str = "activity") -> None:
    await request.app.state.event_bus.publish(
        event_type,
        domain_event(event_type, aggregate, aggregate_id, payload,
                     visibility=visibility),
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
    is the same internal-org lookup the directory uses). 32e: ONE batched
    call replaces the per-id loop (an N+1 against identity on every
    list); any failure falls back to the legacy loop so a hiccup never
    kills the list. Missing names resolve to None and the UI falls back
    to a dash."""
    wanted = {str(i) for i in ids if i is not None}
    if not wanted:
        return {}
    names: dict[str, str | None] = {}
    try:
        async with httpx.AsyncClient(
            base_url=identity_base_url(), timeout=10.0,
            transport=identity_transport(),
        ) as c:
            resp = await c.post("/api/internal/orgs/names",
                                json={"ids": sorted(wanted)})
        if resp.status_code == 200:
            found = resp.json().get("names") or {}
            return {oid: found.get(oid) for oid in wanted}
    except Exception:
        pass
    for oid in wanted:
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
    request: Request,
    status: str = Query("", max_length=20),
    client: str = Query("", max_length=120),
    port: str = Query("", max_length=120),
    vessel: str = Query("", max_length=120),
    ref: str = Query("", max_length=120),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> dict:
    """32e: the tenant View-RFQ list. Tenant scoping (the org-is-involved
    clause) is applied BEFORE every filter — filters only ever narrow the
    caller's own rows. Blank/absent = no restriction; filters compose
    (AND). Dates are created-date, inclusive; a reversed range matches
    nothing rather than erroring. Ordering is deterministic, newest
    first. `client` is a case-insensitive partial match on the resolved
    requesting-org display name (work resolves names via identity — no
    cross-DB reads), falling back to the contact email."""
    caller = await caller_org(request)
    clause = "(requesting_org_id = :org OR target_org_id = :org)"
    params: dict = {"org": caller["org_id"]}
    if status:
        clause += " AND status = :status"
        params["status"] = status
    if port:
        clause += " AND port_name ILIKE :port"
        params["port"] = f"%{port}%"
    if vessel:
        clause += " AND vessel_name ILIKE :vessel"
        params["vessel"] = f"%{vessel}%"
    if ref:
        clause += " AND reference_no ILIKE :ref"
        params["ref"] = f"%{ref}%"
    reversed_dates = (date_from is not None and date_to is not None
                      and date_from > date_to)
    if not reversed_dates:
        if date_from is not None:
            clause += " AND created_at::date >= :dfrom"
            params["dfrom"] = date_from
        if date_to is not None:
            clause += " AND created_at::date <= :dto"
            params["dto"] = date_to
    else:
        clause += " AND false"
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE {clause} "
                     "ORDER BY created_at DESC, id DESC"),
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
    ids = [i for r in rows for i in
           (r["requesting_org_id"], r["target_org_id"], r["billing_org_id"])]
    org_names = await _org_names(ids)
    out = []
    needle = client.strip().lower()
    for r in rows:
        if needle:
            display = (org_names.get(str(r["requesting_org_id"]))
                       or r["contact_email"] or "")
            if needle not in display.lower():
                continue
        d = _row(r)
        d["survey_types"] = by_id.get(str(r["id"]), [])
        out.append(d)
    return {"rfqs": out, "org_names": org_names}


@router.get("/api/rfqs/quote-options")
async def quote_options(request: Request,
                        q: str = Query("", max_length=100)) -> dict:
    """32c: the raise-quote search-select — RFQs the caller may quote,
    freshest first. Only awaiting_quote/quoted rows targeted at the caller
    qualify; q matches the reference/vessel/port and an id prefix."""
    member = await require_permission(request, "quote.create")
    clause = ("target_org_id = :org "
              "AND status IN ('awaiting_quote', 'quoted')")
    params: dict = {"org": member["org_id"]}
    if q:
        clause += (" AND (reference_no ILIKE :q OR vessel_name ILIKE :q OR "
                   "port_name ILIKE :q OR id::text LIKE :prefix)")
        params["q"] = f"%{q}%"
        params["prefix"] = f"{q}%"
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE {clause} "
                     "ORDER BY created_at DESC LIMIT 20"),
                params,
            )
        ).mappings().all()
    names = await _org_names([r["requesting_org_id"] for r in rows])
    out = []
    for r in rows:
        d = _row(r)
        out.append({
            "id": d["id"],
            "reference_no": d["reference_no"],
            "vessel_name": d["vessel_name"],
            "vessel_imo": d["vessel_imo"],
            "status": d["status"],
            "channel": d["channel"],
            "client": names.get(str(r["requesting_org_id"])),
            "created_at": d["created_at"],
        })
    return {"rfqs": out}


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
                text("SELECT * FROM quote WHERE rfq_id = :id "
                     "ORDER BY revision"),
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
    return {**_row(row), "quotes": [_quote_out(q) for q in quotes],
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
                "SELECT q.*, r.target_org_id, r.requesting_org_id, "
                "r.survey_type, r.vessel_imo, r.billing_org_id, "
                "r.status AS rfq_status "
                "FROM quote q JOIN rfq r ON r.id = q.rfq_id WHERE q.id = :id"
            ),
            {"id": quote_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="quote not found")
    return dict(row)


INSERT_QUOTE = (
    "INSERT INTO quote ("
    "id, rfq_id, revision, line_items, currency, valid_until, status, "
    "created_at, tenant_org_id, quote_no, quote_date, updated_at, "
    "reference_no, vessel_name, vessel_imo, survey_types, location_type, "
    "port_name, scope_notes, client_name, client_contact_person, "
    "client_designation, client_address, client_country, client_phone, "
    "client_email, prepared_by, prepared_by_name, prepared_by_email, "
    "terms, bank_details"
    ") VALUES ("
    ":id, :rfq, :rev, CAST(:items AS jsonb), :cur, :valid, 'draft', now(), "
    ":tenant, :qno, :qdate, now(), "
    ":ref, :vname, :imo, CAST(:stypes AS jsonb), :ltype, :pname, :scope, "
    ":cname, :cperson, :cdesig, CAST(:caddr AS jsonb), :ccountry, :cphone, "
    ":cemail, :pby, :pbyname, :pbyemail, CAST(:terms AS jsonb), "
    "CAST(:bank AS jsonb)"
    ")"
)


def _author_stamp(member: dict) -> dict:
    """32c: prepared_by = caller uid; prepared_by_name = the caller's
    email (or "uid:<uid>"); None everywhere when identity gave no uid."""
    uid = member.get("uid")
    if uid is None:
        return {"pby": None, "pbyname": None,
                "pbyemail": member.get("email")}
    return {"pby": str(uid),
            "pbyname": member.get("email") or f"uid:{uid}",
            "pbyemail": member.get("email")}


async def _rfq_survey_types(session, rfq) -> list:
    """Authoritative multi-select codes; the legacy column is the
    fallback for rows that predate the join."""
    codes = (
        await session.execute(
            text("SELECT survey_type FROM rfq_survey_type WHERE rfq_id = :id "
                 "ORDER BY survey_type"),
            {"id": str(rfq["id"])},
        )
    ).scalars().all()
    return list(codes) if codes else [rfq["survey_type"]]


@router.post("/api/rfqs/{rfq_id}/quotes", status_code=201)
async def create_quote(rfq_id: str, payload: QuoteIn, request: Request) -> dict:
    caller = await caller_org(request)
    quote_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        # FOR UPDATE serializes concurrent creates on one RFQ — the
        # MAX(revision)+1 read then cannot collide (32c uniqueness test).
        rfq = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE id = :id FOR UPDATE"),
                {"id": rfq_id},
            )
        ).mappings().first()
        if rfq is None:
            raise HTTPException(status_code=404, detail="rfq not found")
        if str(rfq["target_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the target org quotes")
        member = await require_permission(request, "quote.create")
        tenant = str(rfq["target_org_id"])
        commercial = await _org_commercial(tenant)
        line_items = (_validate_line_items(payload.line_items)
                      if payload.line_items is not None else [])
        quote_date = payload.quote_date or date.today()
        quote_no = await _next_quote_no(session, tenant,
                                        commercial["quote_prefix"],
                                        quote_date.year)
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
        stamp = _author_stamp(member)
        snapshot = await _client_snapshot(session, rfq)
        terms = (payload.terms if payload.terms is not None
                 else commercial["default_terms"] or DEFAULT_TERMS)
        await session.execute(
            text(INSERT_QUOTE),
            {
                "id": quote_id,
                "rfq": rfq_id,
                "rev": next_rev,
                "items": json.dumps(line_items),
                "cur": payload.currency or "USD",
                "valid": payload.valid_until,
                "tenant": tenant,
                "qno": quote_no,
                "qdate": quote_date,
                "ref": rfq["reference_no"],
                "vname": rfq["vessel_name"],
                "imo": rfq["vessel_imo"],
                "stypes": json.dumps(await _rfq_survey_types(session, rfq)),
                "ltype": rfq["location_type"],
                "pname": rfq["port_name"],
                "scope": rfq["scope_notes"],
                "cname": snapshot["client_name"],
                "cperson": snapshot["client_contact_person"],
                "cdesig": snapshot["client_designation"],
                "caddr": (json.dumps(snapshot["client_address"])
                          if snapshot["client_address"] is not None else None),
                "ccountry": snapshot["client_country"],
                "cphone": snapshot["client_phone"],
                "cemail": snapshot["client_email"],
                "pby": stamp["pby"],
                "pbyname": stamp["pbyname"],
                "pbyemail": stamp["pbyemail"],
                "terms": json.dumps(terms),
                "bank": (json.dumps(payload.bank_details)
                         if payload.bank_details is not None
                         else (json.dumps(commercial["payment_details"])
                               if commercial["payment_details"] is not None
                               else None)),
            },
        )
        await session.commit()
        row = await _load_quote_row(session, quote_id)
    await _publish(request, "quote.created", "quote", quote_id,
                   {"quote_id": quote_id, "rfq_id": rfq_id,
                    "revision": next_rev, "quote_no": quote_no,
                    "tenant_org_id": tenant})
    return _quote_out(row)


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(quote_id: str, request: Request) -> dict:
    caller = await caller_org(request)
    async with SessionLocal() as session:
        q = await _load_quote(session, quote_id)
        if str(q["target_org_id"]) != caller["org_id"]:
            raise HTTPException(status_code=403, detail="only the target org sends")
        if q["status"] != "draft":
            raise HTTPException(status_code=409, detail=f"cannot send from {q['status']}")
        # 32c: the legacy send path also stamps the issue gate fields
        await session.execute(
            text("UPDATE quote SET status = 'sent', issued_by = :uid, "
                 "issued_at = now(), updated_at = now() WHERE id = :id"),
            {"id": quote_id, "uid": caller.get("uid")},
        )
        await session.execute(
            text("UPDATE rfq SET status = 'quoted' WHERE id = :id AND "
                 "status IN ('open', 'awaiting_quote')"),
            {"id": str(q["rfq_id"])},
        )
        await session.commit()
    await _publish(request, "quote.sent", "quote", quote_id,
                   {"quote_id": quote_id, "rfq_id": str(q["rfq_id"]),
                    "revision": q["revision"], "quote_no": q["quote_no"]},
                   visibility="both")
    return {"id": quote_id, "status": "sent"}


# ---------------------------------------------------------------- quotes
# document API (32c): list / detail / edit / approve+issue / revision.
# (Declared AFTER the literal sub-paths above; GET /api/quotes/{quote_id}
# cannot shadow anything — no other GET route shares that prefix.)

@router.get("/api/quotes")
async def list_quotes(request: Request,
                      status: str = Query("", max_length=20),
                      q: str = Query("", max_length=100),
                      client: str = Query("", max_length=120),
                      port: str = Query("", max_length=120),
                      vessel: str = Query("", max_length=120),
                      ref: str = Query("", max_length=120),
                      date_from: date | None = Query(None),
                      date_to: date | None = Query(None)) -> dict:
    """32e: the tenant View-Quotes list. tenant_org_id scoping is applied
    BEFORE every filter (org-scoping IS authz); blank = no restriction and
    filters compose (AND). Dates are quote_date, inclusive; a reversed
    range matches nothing. Ordering is deterministic, newest first, and
    there is deliberately NO row cap — the unfiltered list never silently
    truncates."""
    member = await require_permission(request, "quote.view")
    clause = "tenant_org_id = :org"
    params: dict = {"org": member["org_id"]}
    if status:
        clause += " AND status = :status"
        params["status"] = status
    if q:
        clause += (" AND (quote_no ILIKE :q OR client_name ILIKE :q OR "
                   "vessel_name ILIKE :q OR reference_no ILIKE :q)")
        params["q"] = f"%{q}%"
    if client:
        clause += " AND client_name ILIKE :client"
        params["client"] = f"%{client}%"
    if port:
        clause += " AND port_name ILIKE :port"
        params["port"] = f"%{port}%"
    if vessel:
        clause += " AND vessel_name ILIKE :vessel"
        params["vessel"] = f"%{vessel}%"
    if ref:
        clause += " AND reference_no ILIKE :ref"
        params["ref"] = f"%{ref}%"
    reversed_dates = (date_from is not None and date_to is not None
                      and date_from > date_to)
    if not reversed_dates:
        if date_from is not None:
            clause += " AND quote_date >= :dfrom"
            params["dfrom"] = date_from
        if date_to is not None:
            clause += " AND quote_date <= :dto"
            params["dto"] = date_to
    else:
        clause += " AND false"
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(f"SELECT * FROM quote WHERE {clause} "
                     "ORDER BY created_at DESC, id DESC"),
                params,
            )
        ).mappings().all()
    return {"quotes": [_quote_out(r) for r in rows]}


@router.get("/api/quotes/{quote_id}")
async def get_quote(request: Request, quote_id: str) -> dict:
    member = await require_permission(request, "quote.view")
    async with SessionLocal() as session:
        row = await _load_quote_row(session, quote_id)
    if row is None or str(row["tenant_org_id"]) != member["org_id"]:
        raise HTTPException(status_code=404, detail="quote not found")
    return _quote_out(row)


@router.put("/api/quotes/{quote_id}")
async def update_quote(quote_id: str, payload: QuoteUpdate,
                       request: Request) -> dict:
    member = await require_permission(request, "quote.create")
    async with SessionLocal() as session:
        row = await _load_quote_row(session, quote_id)
        if row is None or str(row["tenant_org_id"]) != member["org_id"]:
            raise HTTPException(status_code=404, detail="quote not found")
        if row["status"] != "draft":
            raise HTTPException(status_code=409,
                                detail=f"cannot update from {row['status']}")
        sets = ["updated_at = now()"]
        params: dict = {"id": quote_id}
        if payload.line_items is not None:
            sets.append("line_items = CAST(:items AS jsonb)")
            params["items"] = json.dumps(
                _validate_line_items(payload.line_items))
        if payload.currency is not None:
            sets.append("currency = :cur")
            params["cur"] = payload.currency
        if payload.valid_until is not None:
            sets.append("valid_until = :valid")
            params["valid"] = payload.valid_until
        if payload.quote_date is not None:
            sets.append("quote_date = :qdate")
            params["qdate"] = payload.quote_date
        if payload.terms is not None:
            sets.append("terms = CAST(:terms AS jsonb)")
            params["terms"] = json.dumps(payload.terms)
        if payload.bank_details is not None:
            sets.append("bank_details = CAST(:bank AS jsonb)")
            params["bank"] = json.dumps(payload.bank_details)
        await session.execute(
            text(f"UPDATE quote SET {', '.join(sets)} WHERE id = :id"), params
        )
        await session.commit()
        row = await _load_quote_row(session, quote_id)
    return _quote_out(row)


@router.post("/api/quotes/{quote_id}/approve-issue")
async def approve_issue(quote_id: str, payload: ApproveIssueIn,
                        request: Request) -> dict:
    member = await require_permission(request, "quote.create")
    async with SessionLocal() as session:
        row = await _load_quote_row(session, quote_id)
        if row is None or str(row["tenant_org_id"]) != member["org_id"]:
            raise HTTPException(status_code=404, detail="quote not found")
        if payload.confirm is not True:
            raise HTTPException(status_code=422,
                                detail="confirm must be true")
        if row["status"] != "draft":
            raise HTTPException(status_code=409,
                                detail=f"cannot issue from {row['status']}")
        rfq_status = (
            await session.execute(
                text("SELECT status FROM rfq WHERE id = :id"),
                {"id": str(row["rfq_id"])},
            )
        ).scalar()
        if rfq_status not in ("awaiting_quote", "quoted"):
            raise HTTPException(
                status_code=409,
                detail=f"cannot issue while the RFQ is {rfq_status}")
        # ONE transaction: the conditional UPDATE is the concurrent-issue
        # guard — exactly one caller can flip draft -> sent.
        uid = member.get("uid")
        result = await session.execute(
            text("UPDATE quote SET status = 'sent', approved_by = :uid, "
                 "approved_at = now(), issued_by = :uid, issued_at = now(), "
                 "updated_at = now() WHERE id = :id AND status = 'draft'"),
            {"id": quote_id, "uid": str(uid) if uid is not None else None},
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409,
                                detail="quote was issued concurrently")
        await session.execute(
            text("UPDATE rfq SET status = 'quoted' WHERE id = :id AND "
                 "status IN ('awaiting_quote')"),
            {"id": str(row["rfq_id"])},
        )
        await session.commit()
        row = await _load_quote_row(session, quote_id)
    await _publish(request, "quote.approved", "quote", quote_id,
                   {"quote_id": quote_id, "rfq_id": str(row["rfq_id"]),
                    "revision": row["revision"], "quote_no": row["quote_no"],
                    "approved_by": str(uid) if uid is not None else None},
                   visibility="both")
    await _publish(request, "quote.sent", "quote", quote_id,
                   {"quote_id": quote_id, "rfq_id": str(row["rfq_id"]),
                    "revision": row["revision"], "quote_no": row["quote_no"]},
                   visibility="both")
    return _quote_out(row)


@router.post("/api/quotes/{quote_id}/revision", status_code=201)
async def revise_quote(quote_id: str, request: Request) -> dict:
    """32c: copy a quote into a fresh draft revision — same RFQ, echoes,
    client snapshot, terms and bank block; fresh quote_no and revision;
    prior 'sent' quotes superseded; the RFQ status is UNTOUCHED."""
    member = await require_permission(request, "quote.create")
    new_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        source = await _load_quote_row(session, quote_id)
        if source is None or str(source["tenant_org_id"]) != member["org_id"]:
            raise HTTPException(status_code=404, detail="quote not found")
        rfq_id = str(source["rfq_id"])
        tenant = str(source["tenant_org_id"])
        rfq = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE id = :id FOR UPDATE"),
                {"id": rfq_id},
            )
        ).mappings().first()
        # the RFQ row cannot be missing (quote.rfq_id is an FK) — but never
        # 500 on it either; treat as a plain not-found.
        if rfq is None:
            raise HTTPException(status_code=404, detail="rfq not found")
        commercial = await _org_commercial(tenant)
        quote_date = date.today()
        quote_no = await _next_quote_no(session, tenant,
                                        commercial["quote_prefix"],
                                        quote_date.year)
        next_rev = (
            await session.execute(
                text("SELECT COALESCE(MAX(revision), 0) + 1 FROM quote "
                     "WHERE rfq_id = :id"),
                {"id": rfq_id},
            )
        ).scalar_one()
        await session.execute(
            text("UPDATE quote SET status = 'superseded' "
                 "WHERE rfq_id = :id AND status = 'sent'"),
            {"id": rfq_id},
        )
        stamp = _author_stamp(member)
        await session.execute(
            text(INSERT_QUOTE),
            {
                "id": new_id,
                "rfq": rfq_id,
                "rev": next_rev,
                "items": json.dumps(_json_val(source["line_items"])),
                "cur": source["currency"],
                "valid": source["valid_until"],
                "tenant": tenant,
                "qno": quote_no,
                "qdate": quote_date,
                "ref": source["reference_no"],
                "vname": source["vessel_name"],
                "imo": source["vessel_imo"],
                "stypes": json.dumps(_json_val(source["survey_types"])),
                "ltype": source["location_type"],
                "pname": source["port_name"],
                "scope": source["scope_notes"],
                "cname": source["client_name"],
                "cperson": source["client_contact_person"],
                "cdesig": source["client_designation"],
                "caddr": (json.dumps(_json_val(source["client_address"]))
                          if source["client_address"] is not None else None),
                "ccountry": source["client_country"],
                "cphone": source["client_phone"],
                "cemail": source["client_email"],
                "pby": stamp["pby"],
                "pbyname": stamp["pbyname"],
                "pbyemail": stamp["pbyemail"],
                "terms": json.dumps(_json_val(source["terms"])),
                "bank": (json.dumps(_json_val(source["bank_details"]))
                         if source["bank_details"] is not None else None),
            },
        )
        await session.commit()
        row = await _load_quote_row(session, new_id)
    await _publish(request, "quote.created", "quote", new_id,
                   {"quote_id": new_id, "rfq_id": rfq_id,
                    "revision": next_rev, "quote_no": quote_no,
                    "tenant_org_id": tenant})
    return _quote_out(row)


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
