"""32c: commercial defaults — the org-level quotation settings.

GET/POST /org/commercial (survey_company tenant only): the quote
numbering prefix, the default quotation terms (the prefill for every new
quote), and the bank/payment details block. Data lives in identity
(/api/orgs/me/commercial, org.profile.view / org.profile.edit); the page
is the survey-company settings surface, so the org-type handling mirrors
the profile page. 404-over-403 on every route.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    _redirect,
    _render,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

ORG_PROFILE_VIEW = "org.profile.view"
ORG_PROFILE_EDIT = "org.profile.edit"

BANK_KEYS = ("bank_name", "branch", "account_name",
             "account_number", "iban", "swift")


def _ctx(ctx, membership, org, values, field_errors, *, saved=False,
         form_error=None) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership), "keepalive": _KEEPALIVE,
        "values": values, "field_errors": field_errors, "saved": saved,
        "form_error": form_error, "bank_keys": BANK_KEYS,
    }


async def _commercial(request) -> dict:
    sid = session_id_from(request)
    resp = await identity_request(
        "GET", "/api/orgs/me/commercial",
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    return resp.json()


def _values(commercial: dict) -> dict:
    pay = commercial.get("payment_details") or {}
    return {
        "quote_prefix": commercial.get("quote_prefix") or "",
        "terms": list(commercial.get("default_terms") or []),
        "bank": {k: (str(pay.get(k)) if pay.get(k) is not None else "")
                 for k in BANK_KEYS},
    }


@router.get("/org/commercial", response_class=HTMLResponse)
async def commercial_page(request: Request, saved: str = "",
                          ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect("/dashboard")
    if not _has(membership, ORG_PROFILE_VIEW):
        return _page_not_found(request, ctx)
    org = await _org_summary(membership.slug)
    return _render(
        request, "org_commercial.html",
        _ctx(ctx, membership, org, _values(await _commercial(request)), {},
             saved=saved == "1"),
    )


@router.post("/org/commercial")
async def commercial_save(
    request: Request,
    quote_prefix: str = Form(""),
    terms: list[str] = Form([]),
    bank_name: str = Form(""), branch: str = Form(""),
    account_name: str = Form(""), account_number: str = Form(""),
    iban: str = Form(""), swift: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    if not _has(membership, ORG_PROFILE_EDIT):
        return _page_not_found(request, ctx)

    values = {
        "quote_prefix": quote_prefix.strip().upper(),
        # an empty editor means "no custom defaults" — quotes fall back
        # to the standard terms (identity stores [] as a full replace)
        "terms": [t.strip() for t in terms if t.strip()],
        "bank": {k: v.strip() for k, v in (
            ("bank_name", bank_name), ("branch", branch),
            ("account_name", account_name), ("account_number", account_number),
            ("iban", iban), ("swift", swift)) if v.strip()},
    }
    payload: dict = {"default_terms": values["terms"],
                     "payment_details": values["bank"]}
    # identity PUT: None = leave untouched — an empty prefix omits the
    # field entirely (there is nothing to clear a prefix TO)
    if values["quote_prefix"]:
        payload["quote_prefix"] = values["quote_prefix"]

    sid = session_id_from(request)
    resp = await identity_request(
        "PUT", "/api/orgs/me/commercial", json=payload,
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code == 200:
        return _redirect("/org/commercial?saved=1")

    field_errors: dict[str, str] = {}
    detail = resp.json().get("detail")
    if resp.status_code == 422 and isinstance(detail, list):
        for item in detail:
            msg = str(item.get("msg", ""))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            loc = item.get("loc") or []
            field = str(loc[-1]) if loc else ""
            field_errors[field or "form"] = msg
    org = await _org_summary(membership.slug)
    return _render(
        request, "org_commercial.html",
        _ctx(ctx, membership, org, values, field_errors,
             form_error=field_errors.get("form")),
        status_code=resp.status_code if resp.status_code == 422 else 502,
    )
