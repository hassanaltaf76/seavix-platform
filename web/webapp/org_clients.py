"""32a: the client directory pages — permission-gated, 404-over-403.

- GET  /org/clients           client.view — the directory table
- GET  /org/clients/new       client.create — the add-client form
- POST /org/clients/new       client.create — Save via the sticky bar (PRG)
- GET  /org/clients/{id}      client.view — contact card + RFQ placeholder

The pages proxy the work service, forwarding the caller's session header
(the work service re-resolves permissions from identity and org-scopes
every query). The create form reuses the 31t contact block components
(country + dial-code .sv-search-select comboboxes with flags) via the
shared _sv_search_select.html partial."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from seavix_ports import AuthContext, require_auth

from .auth import SESSION_HEADER, session_id_from
from .countries import COUNTRIES
from .filters import flag
from .filters import install as install_template_filters
from .org_pages import (
    _KEEPALIVE,
    _first_membership,
    _has,
    _page_not_found,
    _rail_org,
    _render,
)
from .work_client import work_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

CLIENT_VIEW = "client.view"
CLIENT_CREATE = "client.create"

# Human labels for the directory relationship vocabulary (work owns the
# codes — client_directory CHECK constraint; these are the render texts).
RELATIONSHIP_LABELS = {
    "management": "Management",
    "surveyor_subcontract": "Surveyor subcontract",
    "port_agent": "Port agent",
}


def _client_ctx(ctx, membership, **extra) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership,
        "rail_org": _rail_org(None, membership), "keepalive": _KEEPALIVE,
        "relationship_labels": RELATIONSHIP_LABELS,
        **extra,
    }


def _sid_headers(request: Request) -> dict:
    sid = session_id_from(request)
    return {SESSION_HEADER: sid} if sid else {}


def _sv_countries() -> list[dict]:
    return [{"name": name, "iso2": a2, "dial": dial, "flag": flag(a2)}
            for name, a2, dial in COUNTRIES]


def _prefill(values: dict, cc_key: str, national_key: str,
             country_code: str | None) -> dict:
    """31u prefill for a dial-code combobox on the create form (no stored
    cc: a legacy "+cc national" value splits on the longest dial prefix,
    matching the profile editor's _edit_context behaviour)."""
    from .countries import split_dial

    cc = values.get(cc_key)
    national = values.get(national_key) or ""
    if not cc:
        cc, national = split_dial(national)
    item = None
    if cc:
        fallback = None
        for name, alpha2, dial in COUNTRIES:
            if dial == cc:
                if country_code and alpha2 == country_code:
                    item = {"name": name, "iso2": alpha2, "dial": dial,
                            "flag": flag(alpha2)}
                    break
                if fallback is None:
                    fallback = {"name": name, "iso2": alpha2, "dial": dial,
                                "flag": flag(alpha2)}
        item = item or fallback
    label = f'{item["flag"]} {cc} — {item["name"]}' if item else ""
    return {"cc": cc or "", "national": national, "item": item, "label": label}


def _new_context(ctx, membership, values, field_errors, form_error=None) -> dict:
    country_code = values.get("country_code") or None
    resolved = None
    if country_code:
        from .countries import country_name

        resolved = country_name(country_code)
    return _client_ctx(
        ctx, membership,
        values=values, field_errors=field_errors, form_error=form_error,
        sv_countries=_sv_countries(),
        country_label=(f"{flag(country_code)} {resolved}" if resolved else ""),
        phone_prefill=_prefill(values, "phone_cc", "phone", country_code),
        whatsapp_prefill=_prefill(values, "whatsapp_cc", "whatsapp", country_code),
    )


@router.get("/org/clients", response_class=HTMLResponse)
async def clients_index(request: Request, created: str = "",
                        ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _has(membership, CLIENT_VIEW):
        return _page_not_found(request, ctx)
    resp = await work_request("GET", "/api/clients", headers=_sid_headers(request))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return _render(
        request, "org_clients.html",
        _client_ctx(ctx, membership, clients=resp.json()["clients"],
                    created=created == "1",
                    can_create=_has(membership, CLIENT_CREATE)),
    )


@router.get("/org/clients/new", response_class=HTMLResponse)
async def client_new(request: Request,
                     ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _has(membership, CLIENT_CREATE):
        return _page_not_found(request, ctx)
    return _render(
        request, "org_client_new.html",
        _new_context(ctx, membership, values={}, field_errors={}),
    )


@router.post("/org/clients/new")
async def client_create(
    request: Request,
    name: str = Form(""),
    relationship_type: str = Form(""),
    email: str = Form(""),
    contact_email: str = Form(""),
    phone_cc: str = Form(""),
    phone: str = Form(""),
    whatsapp_cc: str = Form(""),
    whatsapp: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    postal_code: str = Form(""),
    country_code: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None or not _has(membership, CLIENT_CREATE):
        return _page_not_found(request, ctx)
    values = {
        "name": name, "relationship_type": relationship_type, "email": email,
        "contact_email": contact_email, "phone_cc": phone_cc, "phone": phone,
        "whatsapp_cc": whatsapp_cc, "whatsapp": whatsapp,
        "address_line1": address_line1, "address_line2": address_line2,
        "city": city, "state": state, "postal_code": postal_code,
        "country_code": country_code,
    }
    resp = await work_request(
        "POST", "/api/clients",
        json={k: (v.strip() or None) for k, v in values.items()},
        headers=_sid_headers(request),
    )
    if resp.status_code == 201:
        return RedirectResponse(
            f"/org/clients/{resp.json()['id']}?created=1", status_code=303
        )
    if resp.status_code == 409:
        return _render(
            request, "org_client_new.html",
            _new_context(ctx, membership, values, {},
                         form_error="This client is already in your directory."),
            status_code=409,
        )
    if resp.status_code == 422:
        field_errors: dict[str, str] = {}
        detail = resp.json().get("detail")
        if isinstance(detail, list):
            for item in detail:
                msg = str(item.get("msg", ""))
                if msg.startswith("Value error, "):
                    msg = msg[len("Value error, "):]
                loc = item.get("loc") or []
                field = str(loc[-1]) if loc else ""
                field_errors[field] = msg
        return _render(
            request, "org_client_new.html",
            _new_context(ctx, membership, values, field_errors),
            status_code=422,
        )
    return _render(
        request, "org_client_new.html",
        _new_context(ctx, membership, values, {},
                     form_error="Could not add the client — try again."),
        status_code=502,
    )


@router.get("/org/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: str,
                        created: str = "",
                        ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _has(membership, CLIENT_VIEW):
        return _page_not_found(request, ctx)
    resp = await work_request("GET", f"/api/clients/{client_id}",
                              headers=_sid_headers(request))
    if resp.status_code == 404:
        return _page_not_found(request, ctx)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return _render(
        request, "org_client_detail.html",
        _client_ctx(ctx, membership, client=resp.json(),
                    created=created == "1"),
    )
