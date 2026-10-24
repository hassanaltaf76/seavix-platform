"""32b: the two RFQ work surfaces.

- TENANT (manual channel, "for record"): the COMPANY rail gains "RFQs"
  (rfq.create). The list is the tenant's inbound pipeline (public funnel
  + manual intake); the Raise form links a directory client via the
  .sv-search-select picker or quick-creates one inline; the detail page
  shows the full field set + the client contact card + the Quotes
  placeholder (arrives with 32c).
- CLIENT (registered-client rail): org_type='client' + rfq.view — the
  org's RFQs ACROSS survey companies, status badges only. Work-service
  loaders org-scope every query, so a client-role org never sees another
  client's data (test-proven).

Permission matrix (404-over-403 everywhere): owner/admin/ops hold
rfq.create and manage; surveyor/viewer do NOT — every page here 404s for
them; the client role reaches only the list/detail of its own org.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from seavix_ports import AuthContext, require_auth

from .auth import SESSION_HEADER, session_id_from
from .filters import install as install_template_filters
from .identity_client import identity_request
from .org_pages import (
    _KEEPALIVE,
    _first_membership,
    _has,
    _org_summary,
    _page_not_found,
    _rail_org,
    _render,
)
from .work_client import work_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

RFQ_CREATE = "rfq.create"
RFQ_VIEW = "rfq.view"

# Render labels for the stored vocabularies (work owns the codes).
STATUS_LABELS = {
    "awaiting_quote": "Awaiting quote",
    "quoted": "Quoted",
    "accepted": "Accepted",
    "declined": "Declined",
    "expired": "Expired",
}
CHANNEL_LABELS = {
    "public_page": "Public page",
    "manual_whatsapp": "WhatsApp",
    "manual_email": "Email",
}
LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth",
                  "dry-dock")
# 32c: work owns the quote status codes; the surface owns the labels
# ('sent' reads "Issued" — the document has gone out).
QUOTE_STATUS_LABELS = {
    "draft": "Draft",
    "sent": "Issued",
    "accepted": "Accepted",
    "declined": "Declined",
    "superseded": "Superseded",
}
QUOTE_CREATE = "quote.create"
# Client-directory relationships (work owns the codes; 32a labels).
RELATIONSHIP_LABELS = {
    "management": "Management",
    "surveyor_subcontract": "Surveyor subcontract",
    "port_agent": "Port agent",
}


def _is_client_role(membership) -> bool:
    return (membership is not None
            and getattr(membership, "org_type", None) == "client")


def _can_manage_rfqs(membership) -> bool:
    """The tenant RFQ management surface (list pipeline + Raise form +
    detail). rfq.create AND a non-client org: the registered-client role
    also holds rfq.create (it raises through the public funnel), so the
    org type is the distinguishing gate. Surveyor/viewer lack rfq.create
    entirely — 404 either way."""
    return (membership is not None
            and not _is_client_role(membership)
            and _has(membership, RFQ_CREATE))


def _can_view_rfqs(membership) -> bool:
    # tenant management, or the registered client seeing its own RFQs
    # (rfq.view, org-scoped by the work loaders).
    return _can_manage_rfqs(membership) or (
        _is_client_role(membership) and _has(membership, RFQ_VIEW))


def _sid_headers(request: Request) -> dict:
    sid = session_id_from(request)
    return {SESSION_HEADER: sid} if sid else {}


async def _survey_type_labels(request: Request) -> dict:
    """code -> label from identity's catalog (reference data); raw code on
    any failure — the list never dies on a catalog hiccup."""
    sid = session_id_from(request)
    resp = await identity_request(
        "GET", "/api/catalog/survey-types/search",
        params={"q": ""},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code != 200:
        return {}
    return {e["code"]: e["label"] for e in resp.json().get("survey_types", [])}


def _rfq_ctx(ctx, membership, org=None, **extra) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership,
        "rail_org": _rail_org(org, membership), "keepalive": _KEEPALIVE,
        "status_labels": STATUS_LABELS, "channel_labels": CHANNEL_LABELS,
        "location_types": LOCATION_TYPES,
        "relationship_labels": RELATIONSHIP_LABELS,
        "quote_status_labels": QUOTE_STATUS_LABELS,
        **extra,
    }


async def _raise_ctx(request, ctx, membership, values, field_errors,
                     form_error=None) -> dict:
    """Shared context for the Raise form render + re-renders: survey-type
    chip rehydration (codes -> {code, label}) and the picked client's
    display label (directory edge read back from work)."""
    labels = await _survey_type_labels(request)
    chips = [{"code": c, "label": labels.get(c, c)}
             for c in (values.get("survey_types") or []) if c]
    client_label = ""
    if (values.get("client_mode") == "existing"
            and values.get("client_id")):
        resp = await work_request(
            "GET", f"/api/clients/{values['client_id']}",
            headers=_sid_headers(request),
        )
        if resp.status_code == 200:
            client_label = resp.json()["org"].get("name") or ""
    return _rfq_ctx(ctx, membership,
                    org=await _org_summary(membership.slug),
                    values=values,
                    field_errors=field_errors, form_error=form_error,
                    labels=labels, survey_chips=chips,
                    client_label=client_label)


@router.get("/org/rfqs", response_class=HTMLResponse)
async def rfqs_index(request: Request, created: str = "",
                     ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_view_rfqs(membership):
        return _page_not_found(request, ctx)
    resp = await work_request("GET", "/api/rfqs", headers=_sid_headers(request))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    body = resp.json()
    return _render(
        request, "org_rfqs.html",
        _rfq_ctx(ctx, membership,
                 org=await _org_summary(membership.slug),
                 rfqs=body["rfqs"], org_names=body.get("org_names") or {},
                 labels=await _survey_type_labels(request),
                 is_client=_is_client_role(membership),
                 my_org_id=membership.org_id,
                 can_raise=_can_manage_rfqs(membership),
                 created=created == "1"),
    )


@router.get("/org/rfqs/clients.json")
async def rfq_client_options(request: Request,
                             ctx: AuthContext = Depends(require_auth)):
    """The Raise form's client picker items — the tenant's directory."""
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_rfqs(membership):
        raise HTTPException(status_code=404, detail="not found")
    resp = await work_request("GET", "/api/clients", headers=_sid_headers(request))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return JSONResponse({
        "clients": [
            {"id": c["id"], "name": c["org"].get("name") or "—",
             "relationship": c["relationship_type"]}
            for c in resp.json()["clients"]
        ]
    })


@router.get("/org/rfqs/new", response_class=HTMLResponse)
async def rfq_new(request: Request,
                  ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_rfqs(membership):
        return _page_not_found(request, ctx)
    return _render(
        request, "org_rfq_new.html",
        await _raise_ctx(request, ctx, membership, values={},
                         field_errors={}),
    )


@router.post("/org/rfqs/new")
async def rfq_create(
    request: Request,
    channel: str = Form(""),
    client_mode: str = Form("existing"),
    client_id: str = Form(""),
    new_name: str = Form(""),
    new_relationship: str = Form(""),
    new_email: str = Form(""),
    vessel_name: str = Form(""),
    vessel_imo: str = Form(""),
    survey_types: list[str] = Form([]),
    location_type: str = Form(""),
    port_name: str = Form(""),
    preferred_date: str = Form(""),
    reference_no: str = Form(""),
    scope: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_rfqs(membership):
        return _page_not_found(request, ctx)

    values = {
        "channel": channel, "client_mode": client_mode,
        "client_id": client_id, "new_name": new_name,
        "new_relationship": new_relationship, "new_email": new_email,
        "vessel_name": vessel_name, "vessel_imo": vessel_imo,
        "location_type": location_type, "port_name": port_name,
        "preferred_date": preferred_date, "reference_no": reference_no,
        "scope": scope, "survey_types": survey_types,
    }
    field_errors: dict[str, str] = {}
    codes = [s.strip() for s in survey_types if s.strip()]
    if channel not in ("manual_whatsapp", "manual_email"):
        field_errors["channel"] = "pick how the request arrived"
    if not vessel_name.strip():
        field_errors["vessel_name"] = "vessel name is required"
    if not codes:
        field_errors["survey_types"] = "pick at least one survey type"
    imo = vessel_imo.strip()
    if imo and not re.fullmatch(r"[0-9]{7}", imo):
        field_errors["vessel_imo"] = "the IMO number is 7 digits"
    payload: dict = {
        "channel": channel or None,
        "vessel_name": vessel_name.strip() or None,
        "vessel_imo": imo or None,
        "survey_types": codes or None,
        "location_type": location_type or None,
        "port_name": port_name.strip() or None,
        "preferred_date": preferred_date or None,
        "reference_no": reference_no.strip() or None,
        "scope_notes": scope or None,
    }
    if client_mode == "new":
        if not new_name.strip():
            field_errors["new_name"] = "client name is required"
        if new_relationship not in RELATIONSHIP_LABELS:
            field_errors["new_relationship"] = "pick the relationship"
        if not new_email.strip():
            field_errors["new_email"] = "client email is required"
        payload["new_client"] = {
            "name": new_name.strip() or None,
            "relationship_type": new_relationship or None,
            "email": new_email.strip() or None,
        }
    else:
        if not client_id.strip():
            field_errors["client_id"] = "pick a client from your directory"
        payload["client_id"] = client_id.strip() or None

    async def _rerender(status: int, form_error=None):
        return _render(
            request, "org_rfq_new.html",
            await _raise_ctx(request, ctx, membership, values,
                             field_errors, form_error),
            status_code=status,
        )

    if field_errors:
        return await _rerender(422)

    payload = {k: v for k, v in payload.items() if v is not None}
    resp = await work_request(
        "POST", "/api/rfqs/manual", json=payload,
        headers=_sid_headers(request),
    )
    if resp.status_code == 201:
        return RedirectResponse(
            f"/org/rfqs/{resp.json()['id']}?created=1", status_code=303
        )
    if resp.status_code == 404:
        field_errors["client_id"] = "that client is no longer in your directory"
        return await _rerender(404)
    if resp.status_code == 422:
        detail = resp.json().get("detail")
        if isinstance(detail, list):
            for item in detail:
                msg = str(item.get("msg", ""))
                if msg.startswith("Value error, "):
                    msg = msg[len("Value error, "):]
                loc = item.get("loc") or []
                field = str(loc[-1]) if loc else ""
                field_errors[field or "form"] = msg
        return await _rerender(422)
    return await _rerender(502, form_error="Could not raise the RFQ — try again.")


@router.get("/org/rfqs/{rfq_id}", response_class=HTMLResponse)
async def rfq_detail(request: Request, rfq_id: str, created: str = "",
                     ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_view_rfqs(membership):
        return _page_not_found(request, ctx)
    resp = await work_request("GET", f"/api/rfqs/{rfq_id}",
                              headers=_sid_headers(request))
    if resp.status_code == 404:
        return _page_not_found(request, ctx)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    rfq = resp.json()
    # client contact card: only when the RFQ carries a directory edge and
    # the caller can read the directory (404 -> card omitted, page fine)
    client = None
    if rfq.get("client_id"):
        cresp = await work_request(
            "GET", f"/api/clients/{rfq['client_id']}",
            headers=_sid_headers(request),
        )
        if cresp.status_code == 200:
            client = cresp.json()
    return _render(
        request, "org_rfq_detail.html",
        _rfq_ctx(ctx, membership,
                 org=await _org_summary(membership.slug),
                 rfq=rfq,
                 org_names=rfq.get("org_names") or {},
                 labels=await _survey_type_labels(request),
                 client=client, created=created == "1",
                 is_client=_is_client_role(membership),
                 my_org_id=membership.org_id,
                 can_quote=(not _is_client_role(membership)
                            and _has(membership, QUOTE_CREATE))),
    )
