"""RFQ → quote → job spine: endpoints + state machine.

State rules (enforced here, tested in tests/test_work.py):
- A sent quote is immutable: there is NO update path for line_items; a new
  revision supersedes prior sent quotes.
- Creating a quote: revision = max+1, any 'sent' quotes on the RFQ become
  'superseded', new quote is 'draft'.
- Send: draft → sent (quote.sent); RFQ open → quoted.
- Accept (single transaction): quote sent → accepted; sibling quotes
  (sent/draft) → superseded; RFQ → accepted; job row inserted. Emits
  quote.accepted and job.created.
- Decline: sent → declined.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from seavix_ports import domain_event

from .auth import caller_org, resolve_public_org
from .db import SessionLocal

router = APIRouter()

SURVEY_TYPES = ("pre-purchase", "condition", "bunker", "loading-discharging")
LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth", "dry-dock")

RFQ_COLS = (
    "id, requesting_org_id, billing_org_id, target_org_id, contact_email, "
    "vessel_imo, survey_type, location_type, preferred_date, scope_notes, "
    "source, status, created_at"
)


class RfqIn(BaseModel):
    target_org_id: str
    vessel_imo: str | None = None
    survey_type: Literal[*SURVEY_TYPES]
    location_type: Literal[*LOCATION_TYPES]
    preferred_date: date | None = None
    scope_notes: str | None = None
    contact_email: EmailStr | None = None
    billing_org_id: str | None = None


class PublicRfqIn(BaseModel):
    org_slug: str = Field(min_length=2, max_length=64)
    contact_email: EmailStr
    vessel_imo: str | None = None
    survey_type: Literal[*SURVEY_TYPES]
    location_type: Literal[*LOCATION_TYPES]
    preferred_date: date | None = None
    scope_notes: str | None = None


class QuoteIn(BaseModel):
    line_items: list[dict] = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    valid_until: date | None = None


def _row(r) -> dict:
    d = dict(r)
    for k in ("requesting_org_id", "billing_org_id", "target_org_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    if d.get("preferred_date") is not None:
        d["preferred_date"] = str(d["preferred_date"])
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
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

@router.post("/api/rfqs", status_code=201)
async def create_rfq(payload: RfqIn, request: Request) -> dict:
    caller = await caller_org(request)
    rfq_id = str(uuid.uuid4())
    billing = payload.billing_org_id or caller["org_id"]  # (b) default billing
    async with SessionLocal() as session:
        await session.execute(
            text(
                f"INSERT INTO rfq ({RFQ_COLS}) VALUES "
                "(:id, :req, :bill, :target, :contact, :imo, :stype, :ltype, "
                " :pdate, :scope, 'internal', 'open', now())"
            ),
            {
                "id": rfq_id,
                "req": caller["org_id"],
                "bill": billing,
                "target": payload.target_org_id,
                "contact": payload.contact_email,
                "imo": payload.vessel_imo,
                "stype": payload.survey_type,
                "ltype": payload.location_type,
                "pdate": payload.preferred_date,
                "scope": payload.scope_notes,
            },
        )
        await session.commit()
    await _publish(request, "rfq.created", "rfq", rfq_id,
                   {"target_org_id": payload.target_org_id,
                    "survey_type": payload.survey_type})
    return {"id": rfq_id, "status": "open"}


@router.post("/api/public/rfqs", status_code=201)
async def create_public_rfq(payload: PublicRfqIn, request: Request) -> dict:
    org = await resolve_public_org(payload.org_slug)
    rfq_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        await session.execute(
            text(
                f"INSERT INTO rfq ({RFQ_COLS}) VALUES "
                "(:id, NULL, NULL, :target, :contact, :imo, :stype, :ltype, "
                " :pdate, :scope, 'public', 'open', now())"
            ),
            {
                "id": rfq_id,
                "target": org["org_id"],
                "contact": payload.contact_email,
                "imo": payload.vessel_imo,
                "stype": payload.survey_type,
                "ltype": payload.location_type,
                "pdate": payload.preferred_date,
                "scope": payload.scope_notes,
            },
        )
        await session.commit()
    await _publish(request, "rfq.created", "rfq", rfq_id,
                   {"target_slug": payload.org_slug,
                    "survey_type": payload.survey_type})
    return {"id": rfq_id, "status": "open"}


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
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE {clause} ORDER BY created_at"),
                params,
            )
        ).mappings().all()
    return {"rfqs": [_row(r) for r in rows]}


@router.get("/api/rfqs/{rfq_id}")
async def get_rfq(request: Request, rfq_id: str) -> dict:
    await caller_org(request)  # authenticated read; scaffold-level authz
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(f"SELECT {RFQ_COLS} FROM rfq WHERE id = :id"), {"id": rfq_id}
            )
        ).mappings().first()
        if row is None:
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
    return {**_row(row), "quotes": [_quote_row(q) for q in quotes]}


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
            text("UPDATE rfq SET status = 'quoted' WHERE id = :id AND status = 'open'"),
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
