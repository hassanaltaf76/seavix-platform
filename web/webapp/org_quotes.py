"""32c: the quotation surfaces — list, raise, edit, document, issue gate.

- TENANT ONLY. The registered-client role never reaches these pages (the
  client sees its RFQ's quote STATUS read-only on the RFQ detail — work
  org-scopes the embedded quote rows; the web layer renders them without
  links).
- 404-over-403 everywhere (task-23): list/detail need quote.view; raise/
  edit/issue-confirm/revision need quote.create. Client orgs 404 on all.
- The pages proxy the work service with the caller's session forwarded
  (work re-resolves permissions and org-scopes every query); commercial
  defaults (terms/bank/numbering prefill) come from identity's
  /api/orgs/me/commercial, best-effort — a hiccup there never kills the
  quote form (work applies its own Pak Marine fallbacks on omission).
- Status display: work stores 'sent'; the surface reads "Issued".
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
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
from .org_rfqs import _parse_date, _survey_type_labels
from .work_client import work_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

QUOTE_VIEW = "quote.view"
QUOTE_CREATE = "quote.create"

# Work owns the codes; the surface owns the render labels. 'sent' reads
# "Issued" — the document has gone out, it is no longer a mere "sent" row.
QUOTE_STATUS_LABELS = {
    "draft": "Draft",
    "sent": "Issued",
    "accepted": "Accepted",
    "declined": "Declined",
    "superseded": "Superseded",
}

BANK_KEYS = ("bank_name", "branch", "account_name",
             "account_number", "iban", "swift")

_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")


def _is_client_role(membership) -> bool:
    return (membership is not None
            and getattr(membership, "org_type", None) == "client")


def _can_view_quotes(membership) -> bool:
    return (membership is not None and not _is_client_role(membership)
            and _has(membership, QUOTE_VIEW))


def _can_manage_quotes(membership) -> bool:
    return (membership is not None and not _is_client_role(membership)
            and _has(membership, QUOTE_CREATE))


def _sid_headers(request: Request) -> dict:
    sid = session_id_from(request)
    return {SESSION_HEADER: sid} if sid else {}


def _quote_ctx(ctx, membership, org=None, **extra) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership,
        "rail_org": _rail_org(org, membership), "keepalive": _KEEPALIVE,
        "quote_status_labels": QUOTE_STATUS_LABELS,
        **extra,
    }


async def _commercial_defaults(request: Request) -> dict:
    """The org's quotation defaults from identity (terms/bank prefill).
    Best-effort: any failure yields empty defaults, in which case the
    raise form submits terms/bank OMITTED and work applies the org-level
    (or Pak Marine) fallbacks itself."""
    resp = await identity_request(
        "GET", "/api/orgs/me/commercial", headers=_sid_headers(request)
    )
    if resp.status_code != 200:
        return {"quote_prefix": None, "default_terms": None,
                "payment_details": None}
    return resp.json()


def _money_ok(raw: str) -> bool:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return False
    return (value.is_finite() and value >= 0
            and value.as_tuple().exponent >= -2)


def _reshape_line_items(li_desc: list[str], li_amt: list[str],
                        field_errors: dict) -> list[dict] | None:
    """li_desc/li_amt indexed pairs -> work's line_items. Empty rows
    (both fields blank) drop out; a description without an amount (or a
    bad amount) is a field error. Amounts ride as the raw stripped
    strings — work validates and stores them verbatim."""
    items: list[dict] = []
    for desc, amt in zip(li_desc, li_amt):
        d, a = desc.strip(), amt.strip()
        if not d and not a:
            continue
        if not d:
            field_errors["line_items"] = "every line needs a description"
            return None
        if not a:
            field_errors["line_items"] = "every line needs an amount"
            return None
        if not _money_ok(a):
            field_errors["line_items"] = (
                "amounts are numbers with at most 2 decimals, not negative")
            return None
        items.append({"description": d, "amount": a})
    return items


def _reshape_terms(terms: list[str]) -> list[str]:
    return [t.strip() for t in terms if t.strip()]


def _reshape_bank(values: dict) -> dict:
    return {k: v.strip() for k, v in values.items() if v.strip()}


async def _load_quote_or_404(request, quote_id: str):
    """Work quote detail; None when missing/cross-tenant (work org-scopes
    and answers a plain 404 — the page maps it to the friendly one)."""
    resp = await work_request("GET", f"/api/quotes/{quote_id}",
                              headers=_sid_headers(request))
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return resp.json()


def _quote_form_values(quote: dict | None, commercial: dict,
                       rfq: dict | None) -> dict:
    """The raise/edit form prefill. A fresh raise defaults the dates
    (today / +30 days) and currency; terms prefill from the ORG's
    commercial defaults when present (empty -> omitted on submit so work
    applies its fallback); bank fields prefill from the org payment
    details. An edit starts from the quote's own document instead."""
    today = date.today()
    if quote is not None:
        bank = quote.get("bank_details") or {}
        return {
            "rfq_id": str(quote["rfq_id"]),
            "quote_date": quote.get("quote_date") or str(today),
            "valid_until": quote.get("valid_until") or "",
            "currency": quote.get("currency") or "USD",
            "line_items": quote.get("line_items") or [],
            "terms": quote.get("terms") or [],
            "bank": {k: bank.get(k) or "" for k in BANK_KEYS},
        }
    terms = commercial.get("default_terms") or []
    pay = commercial.get("payment_details") or {}
    return {
        "rfq_id": (rfq or {}).get("id") or "",
        "quote_date": str(today),
        "valid_until": str(today + timedelta(days=30)),
        "currency": "USD",
        "line_items": [],
        "terms": list(terms),
        "bank": {k: (str(pay.get(k)) if pay.get(k) is not None else "")
                 for k in BANK_KEYS},
    }


# ------------------------------------------------------------------ list

@router.get("/org/quotes", response_class=HTMLResponse)
async def quotes_index(request: Request, client: str = "", status: str = "",
                       port: str = "", vessel: str = "", ref: str = "",
                       date_from: str = "", date_to: str = "",
                       ctx: AuthContext = Depends(require_auth)):
    """32e: default-ALL tenant quotations (drafts, issued, accepted,
    declined, superseded — deterministic newest first). The filters
    compose and forward to work, which applies tenant_org_id scoping
    BEFORE every filter; invalid dates are dropped and a reversed range
    renders the friendly no-results state instead of an error."""
    membership = _first_membership(ctx)
    if membership is None or not _can_view_quotes(membership):
        return _page_not_found(request, ctx)
    filters: dict[str, str] = {}
    for key, raw in (("client", client), ("status", status),
                     ("port", port), ("vessel", vessel), ("ref", ref)):
        if raw.strip():
            filters[key] = raw.strip()
    df, dt = _parse_date(date_from), _parse_date(date_to)
    date_notice = ""
    if (date_from.strip() and df is None) or (date_to.strip() and dt is None):
        date_notice = "An invalid date filter was ignored."
    if df is not None:
        filters["date_from"] = str(df)
    if dt is not None:
        filters["date_to"] = str(dt)
    resp = await work_request("GET", "/api/quotes", params=filters,
                              headers=_sid_headers(request))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return _render(
        request, "org_quotes.html",
        _quote_ctx(ctx, membership,
                   org=await _org_summary(membership.slug),
                   quotes=resp.json()["quotes"],
                   can_manage=_can_manage_quotes(membership),
                   filters={"client": client, "status": status, "port": port,
                            "vessel": vessel, "ref": ref,
                            "date_from": date_from, "date_to": date_to},
                   filters_active=bool(filters), date_notice=date_notice),
    )


@router.get("/org/quotes/rfq-options.json")
async def quote_rfq_options(request: Request, q: str = "",
                            ctx: AuthContext = Depends(require_auth)):
    """The raise form's RFQ picker feed — proxies work's quote-options
    (awaiting_quote/quoted RFQs targeted at the caller)."""
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        raise HTTPException(status_code=404, detail="not found")
    resp = await work_request("GET", "/api/rfqs/quote-options",
                              params={"q": q}, headers=_sid_headers(request))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="work service error")
    return JSONResponse(resp.json())


# ------------------------------------------------------------------ raise

async def _raise_form_ctx(request, ctx, membership, rfq, quote, commercial,
                          values, field_errors, form_error=None,
                          created=False) -> dict:
    labels = await _survey_type_labels(request)
    survey_chips, client_label = [], ""
    if quote is not None:
        survey_chips = [{"code": c, "label": labels.get(c, c)}
                        for c in (quote.get("survey_types") or []) if c]
        client_label = quote.get("client_name") or ""
    elif rfq is not None:
        survey_chips = [{"code": c, "label": labels.get(c, c)}
                        for c in (rfq.get("survey_types") or []) if c]
        org_names = rfq.get("org_names") or {}
        client_label = (org_names.get(rfq.get("requesting_org_id"))
                        or rfq.get("contact_email") or "")
    return _quote_ctx(ctx, membership,
                      org=await _org_summary(membership.slug),
                      rfq=rfq, quote=quote, values=values,
                      field_errors=field_errors, form_error=form_error,
                      commercial=commercial, labels=labels,
                      survey_chips=survey_chips, client_label=client_label,
                      created=created)


async def _quote_form_payload(values, field_errors, *, editing: bool):
    """Shared validation/reshape for raise + edit: the form values
    (template shape) into work's payload. Returns the payload, or None
    when field errors were found (the caller re-renders)."""
    currency = values["currency"].strip().upper()
    if currency and not _CURRENCY_RE.fullmatch(currency):
        field_errors["currency"] = "use a 3-letter code, e.g. USD"
    items = _reshape_line_items(
        [i.get("description", "") for i in values["line_items"]],
        [i.get("amount", "") for i in values["line_items"]],
        field_errors)
    terms = _reshape_terms(values["terms"])
    bank = _reshape_bank(values["bank"])

    if field_errors:
        return None

    payload: dict = {
        "quote_date": values["quote_date"] or None,
        "valid_until": values["valid_until"] or None,
        "currency": currency or None,
        "line_items": items,
    }
    # Terms: an empty editor submits NOTHING on a fresh raise — work then
    # applies the org default_terms (or the Pak Marine standard). An edit
    # always sends the list (PUT's None means "not provided" and can
    # never clear; sending [] would wipe the block, so we send exactly
    # what the editor shows — the document's own terms by default).
    if terms or editing:
        payload["terms"] = terms
    # Bank: same split — omitted on a fresh raise (org payment_details
    # fallback); sent on edit so the editor is the document's bank block.
    if bank or editing:
        payload["bank_details"] = bank
    return payload


def _submitted_values(rfq_id, quote_date, valid_until, currency,
                      li_desc, li_amt, terms, bank) -> dict:
    """The raw submitted form fields in the TEMPLATE shape (the re-render
    keeps what the user typed); line-item rows pair up by index."""
    return {
        "rfq_id": (rfq_id or "").strip(), "quote_date": quote_date,
        "valid_until": valid_until, "currency": currency,
        "line_items": [{"description": d, "amount": a}
                       for d, a in zip(li_desc, li_amt)],
        "terms": list(terms), "bank": dict(bank),
    }


@router.get("/org/quotes/raise", response_class=HTMLResponse)
async def quote_raise(request: Request, rfq_id: str = "",
                      ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    rfq = None
    if rfq_id:
        resp = await work_request("GET", f"/api/rfqs/{rfq_id}",
                                  headers=_sid_headers(request))
        if resp.status_code == 404:
            return _page_not_found(request, ctx)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="work service error")
        rfq = resp.json()
    commercial = await _commercial_defaults(request)
    values = _quote_form_values(None, commercial, rfq)
    return _render(
        request, "org_quote_form.html",
        await _raise_form_ctx(request, ctx, membership, rfq, None,
                              commercial, values, {}),
    )


@router.post("/org/quotes/raise")
async def quote_raise_save(
    request: Request,
    rfq_id: str = Form(""),
    quote_date: str = Form(""),
    valid_until: str = Form(""),
    currency: str = Form("USD"),
    li_desc: list[str] = Form([]),
    li_amt: list[str] = Form([]),
    terms: list[str] = Form([]),
    bank_name: str = Form(""), branch: str = Form(""),
    account_name: str = Form(""), account_number: str = Form(""),
    iban: str = Form(""), swift: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)

    commercial = await _commercial_defaults(request)
    values = _submitted_values(
        rfq_id, quote_date, valid_until, currency, li_desc, li_amt, terms,
        {"bank_name": bank_name, "branch": branch,
         "account_name": account_name, "account_number": account_number,
         "iban": iban, "swift": swift})
    field_errors: dict[str, str] = {}
    if not values["rfq_id"]:
        field_errors["rfq_id"] = "pick the RFQ this quote answers"

    payload = await _quote_form_payload(values, field_errors, editing=False)
    if payload is None or field_errors:
        return await _reraise(request, ctx, membership, values,
                              field_errors, commercial)

    resp = await work_request(
        "POST", f"/api/rfqs/{values['rfq_id']}/quotes", json=payload,
        headers=_sid_headers(request),
    )
    if resp.status_code == 201:
        qid = resp.json()["id"]
        return RedirectResponse(f"/org/quotes/{qid}?created=1",
                                status_code=303)
    if resp.status_code in (403, 404):
        return _page_not_found(request, ctx)
    if resp.status_code == 422:
        _absorb_work_errors(resp, field_errors)
        return await _reraise(request, ctx, membership, values,
                              field_errors, commercial, status=422)
    return await _reraise(request, ctx, membership, values, field_errors,
                          commercial, form_error="Could not save the quote — "
                          "try again.", status=502)


async def _reraise(request, ctx, membership, values, field_errors,
                   commercial, form_error=None, status=422):
    rfq = None
    if values.get("rfq_id"):
        resp = await work_request("GET", f"/api/rfqs/{values['rfq_id']}",
                                  headers=_sid_headers(request))
        if resp.status_code == 200:
            rfq = resp.json()
    return _render(
        request, "org_quote_form.html",
        await _raise_form_ctx(request, ctx, membership, rfq, None,
                              commercial, values, field_errors, form_error),
        status_code=status,
    )


def _absorb_work_errors(resp, field_errors: dict) -> None:
    """Work's 422 detail (pydantic list or plain string) -> field errors."""
    detail = resp.json().get("detail")
    if isinstance(detail, list):
        for item in detail:
            msg = str(item.get("msg", ""))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            loc = item.get("loc") or []
            field = str(loc[-1]) if loc else ""
            field_errors[field or "form"] = msg
    elif detail:
        field_errors["form"] = str(detail)


# ----------------------------------------------------------------- detail

@router.get("/org/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail(request: Request, quote_id: str, created: str = "",
                       saved: str = "", issued: str = "", issue_error: str = "",
                       ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_view_quotes(membership):
        return _page_not_found(request, ctx)
    quote = await _load_quote_or_404(request, quote_id)
    if quote is None:
        return _page_not_found(request, ctx)
    org = await _org_summary(membership.slug)
    labels = await _survey_type_labels(request)
    return _render(
        request, "org_quote_detail.html",
        _quote_ctx(ctx, membership, org=org, quote=quote,
                   org_summary=org, labels=labels,
                   can_manage=_can_manage_quotes(membership),
                   created=created == "1", saved=saved == "1",
                   issued=issued == "1", issue_error=issue_error == "1"),
    )


# ------------------------------------------------------------------- edit

@router.get("/org/quotes/{quote_id}/edit", response_class=HTMLResponse)
async def quote_edit(request: Request, quote_id: str,
                     created: str = "",
                     ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    quote = await _load_quote_or_404(request, quote_id)
    if quote is None:
        return _page_not_found(request, ctx)
    if quote["status"] != "draft":
        # draft-only surface; work would 409 — the page simply returns
        # to the document (the Edit action is not offered there either)
        return RedirectResponse(f"/org/quotes/{quote_id}", status_code=303)
    commercial = await _commercial_defaults(request)
    values = _quote_form_values(quote, commercial, None)
    return _render(
        request, "org_quote_form.html",
        await _raise_form_ctx(request, ctx, membership, None, quote,
                              commercial, values, {},
                              created=created == "1"),
    )


@router.post("/org/quotes/{quote_id}/edit")
async def quote_edit_save(
    request: Request, quote_id: str,
    quote_date: str = Form(""),
    valid_until: str = Form(""),
    currency: str = Form("USD"),
    li_desc: list[str] = Form([]),
    li_amt: list[str] = Form([]),
    terms: list[str] = Form([]),
    bank_name: str = Form(""), branch: str = Form(""),
    account_name: str = Form(""), account_number: str = Form(""),
    iban: str = Form(""), swift: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    quote = await _load_quote_or_404(request, quote_id)
    if quote is None:
        return _page_not_found(request, ctx)
    if quote["status"] != "draft":
        return RedirectResponse(f"/org/quotes/{quote_id}", status_code=303)

    commercial = await _commercial_defaults(request)
    values = _submitted_values(
        quote["rfq_id"], quote_date, valid_until, currency, li_desc, li_amt,
        terms,
        {"bank_name": bank_name, "branch": branch,
         "account_name": account_name, "account_number": account_number,
         "iban": iban, "swift": swift})
    field_errors: dict[str, str] = {}
    payload = await _quote_form_payload(values, field_errors, editing=True)
    if payload is None or field_errors:
        return await _reedit(request, ctx, membership, quote, values,
                             field_errors, commercial)

    resp = await work_request("PUT", f"/api/quotes/{quote_id}", json=payload,
                              headers=_sid_headers(request))
    if resp.status_code == 200:
        return RedirectResponse(f"/org/quotes/{quote_id}?saved=1",
                                status_code=303)
    if resp.status_code == 409:
        return RedirectResponse(f"/org/quotes/{quote_id}", status_code=303)
    if resp.status_code == 422:
        _absorb_work_errors(resp, field_errors)
        return await _reedit(request, ctx, membership, quote, values,
                             field_errors, commercial, status=422)
    return await _reedit(request, ctx, membership, quote, values,
                         field_errors, commercial,
                         form_error="Could not save the quote — try again.",
                         status=502)


async def _reedit(request, ctx, membership, quote, values, field_errors,
                  commercial, form_error=None, status=422):
    return _render(
        request, "org_quote_form.html",
        await _raise_form_ctx(request, ctx, membership, None, quote,
                              commercial, values, field_errors, form_error),
        status_code=status,
    )


# ------------------------------------------------------- approve & issue

@router.get("/org/quotes/{quote_id}/issue", response_class=HTMLResponse)
async def quote_issue(request: Request, quote_id: str,
                      ctx: AuthContext = Depends(require_auth)):
    """The explicit human gate — a confirmation page, not a one-click."""
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    quote = await _load_quote_or_404(request, quote_id)
    if quote is None:
        return _page_not_found(request, ctx)
    if quote["status"] != "draft":
        return RedirectResponse(f"/org/quotes/{quote_id}", status_code=303)
    return _render(
        request, "org_quote_issue.html",
        _quote_ctx(ctx, membership,
                   org=await _org_summary(membership.slug), quote=quote),
    )


@router.post("/org/quotes/{quote_id}/issue")
async def quote_issue_confirm(request: Request, quote_id: str,
                              confirm: str = Form(""),
                              ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    quote = await _load_quote_or_404(request, quote_id)
    if quote is None:
        return _page_not_found(request, ctx)
    if confirm != "true":
        return RedirectResponse(f"/org/quotes/{quote_id}", status_code=303)
    resp = await work_request(
        "POST", f"/api/quotes/{quote_id}/approve-issue",
        json={"confirm": True}, headers=_sid_headers(request),
    )
    if resp.status_code == 200:
        return RedirectResponse(f"/org/quotes/{quote_id}?issued=1",
                                status_code=303)
    if resp.status_code == 409:
        # already issued (a concurrent click, or the RFQ moved on) — back
        # to the document with an honest error flash
        return RedirectResponse(f"/org/quotes/{quote_id}?issue_error=1",
                                status_code=303)
    raise HTTPException(status_code=502, detail="work service error")


# ---------------------------------------------------------------- revision

@router.post("/org/quotes/{quote_id}/revision")
async def quote_revision(request: Request, quote_id: str,
                         ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None or not _can_manage_quotes(membership):
        return _page_not_found(request, ctx)
    resp = await work_request("POST", f"/api/quotes/{quote_id}/revision",
                              headers=_sid_headers(request))
    if resp.status_code == 201:
        new_id = resp.json()["id"]
        return RedirectResponse(f"/org/quotes/{new_id}/edit?created=1",
                                status_code=303)
    if resp.status_code == 404:
        return _page_not_found(request, ctx)
    raise HTTPException(status_code=502, detail="work service error")
