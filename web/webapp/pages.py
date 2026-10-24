"""Public pages: landing, registration, org profile, RFQ intake, dev media."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
from seavix_ports import public_route
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    SESSION_HEADER,
    authenticated_home_redirect,
    session_context,
    session_id_from,
)
from .config import blob_root
from .filters import install as install_template_filters
from .identity_client import identity_request
from .ratelimit import RateLimiter
from .registry_client import resolve_ports
from .work_client import work_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)
limiter = RateLimiter(limit=5, window_seconds=3600)

LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth", "dry-dock")
ORG_TYPES = ("agent", "management", "survey_company", "client")


# ------------------------------------------------------------------ 32d
# Public survey-type catalog proxies for the RFQ form's chip picker. The
# form is anonymous, so these ride identity's PUBLIC catalog endpoints
# (reference data; custom entries created here carry NULL provenance).
@router.get("/org/rfq/survey-types/search")
@public_route
async def rfq_survey_types_search(q: str = "") -> dict:
    resp = await identity_request(
        "GET", "/api/public/catalog/survey-types/search", params={"q": q}
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity unavailable")
    return resp.json()


@router.post("/org/rfq/survey-types/create")
@public_route
async def rfq_survey_type_create(request: Request) -> dict:
    body = await request.json()
    resp = await identity_request(
        "POST", "/api/public/catalog/survey-types", json=body
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="identity unavailable")
    return JSONResponse(resp.json(), status_code=resp.status_code)


async def _identity_get(path: str) -> httpx.Response:
    return await identity_request("GET", path)


async def _post_public_rfq(payload: dict, headers: dict | None = None) -> httpx.Response:
    """RFQ intake lives in the work service (spine)."""
    return await work_request("POST", "/api/public/rfqs", json=payload,
                              headers=headers)


async def _rfq_submitter(request: Request) -> dict | None:
    """32b: the RFQ routes are public (the middleware skips auth
    resolution there), so the submit guard asks identity directly —
    a valid session means the funnel continues, none means register.
    """
    return await session_context(request)


# ---------------------------------------------------------------- landing

@router.get("/", response_class=HTMLResponse)
@public_route
async def landing(request: Request) -> HTMLResponse:
    # 31c landing rule: the marketing landing is signed-out-only chrome;
    # a signed-in user goes straight to their workspace home.
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(request, "landing.html", {"request": request})


# ---------------------------------------------------------------- register

@router.get("/register", response_class=HTMLResponse)
@public_route
async def register_form(request: Request, next: str = "") -> HTMLResponse:
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    from .auth import safe_next

    target = safe_next(next)
    return templates.TemplateResponse(
        request, "register.html",
        {"request": request, "error": None, "form": {},
         "next": target,
         # 32b funnel: arriving from an RFQ form, the registrant is a ship
         # operator — preselect the client type for them.
         "rfq_funnel": bool(target) and target.startswith("/org/")
         and target.endswith("/rfq")},
    )


@router.post("/register", response_class=HTMLResponse)
@public_route
async def register_submit(
    request: Request,
    org_name: str = Form(...),
    org_type: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    next: str = Form(""),
) -> HTMLResponse:
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    from .auth import safe_next

    target = safe_next(next)
    form = {"org_name": org_name, "org_type": org_type, "email": email}
    if org_type not in ORG_TYPES:
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": "please choose an organization type",
             "form": form, "next": target,
             "rfq_funnel": bool(target) and target.startswith("/org/")
             and target.endswith("/rfq")},
            status_code=400,
        )
    if password != confirm_password:
        # Client + server guard: no account is created on a mismatch.
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": "Passwords do not match.", "form": form,
             "next": target,
             "rfq_funnel": bool(target) and target.startswith("/org/")
             and target.endswith("/rfq")},
            status_code=400,
        )
    resp = await identity_request(
        "POST", "/api/register",
        json={**form, "password": password, "confirm_password": confirm_password},
    )
    if resp.status_code == 201:
        # Email verification is required before the profile can go live;
        # send the first code immediately and move the user to the code step.
        # 32b: the funnel's return target rides the verification link.
        send_body: dict = {"email": email.lower()}
        if target:
            send_body["next"] = target
        await identity_request(
            "POST", "/api/register/verify/send", json=send_body
        )
        return templates.TemplateResponse(
            request,
            "verify_email.html",
            {"request": request, "email": email.lower(), "error": None,
             "resent": False, "next": target},
        )
    if resp.status_code == 409:
        detail = resp.json().get("detail", {})
        msg = f"{detail.get('field', 'field')}: {detail.get('message', 'conflict')}"
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": msg, "form": form, "next": target,
             "rfq_funnel": bool(target) and target.startswith("/org/")
             and target.endswith("/rfq")},
            status_code=409,
        )
    if resp.status_code == 422:
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": "please check the form fields",
             "form": form, "next": target,
             "rfq_funnel": bool(target) and target.startswith("/org/")
             and target.endswith("/rfq")},
            status_code=400,
        )
    raise HTTPException(status_code=502, detail="registration service error")


# ---------------------------------------------------------------- profiles
# Public profiles live at the root: /<slug>. /p/<slug> is the legacy shape
# and 301-redirects. Concrete app paths (login, register, api, …) are
# registered before the root catch-all and always win; unknown roots fall
# through to the not-available page (404).

@router.get("/p/{slug}", response_class=HTMLResponse)
@public_route
async def legacy_profile_redirect(slug: str) -> RedirectResponse:
    """Legacy /p/<slug> 301s to the canonical /org/<slug> (chain preserved)."""
    return RedirectResponse(f"/org/{slug}", status_code=301)


@router.get("/p/{slug}/rfq", response_class=HTMLResponse)
@public_route
async def legacy_rfq_redirect(slug: str) -> RedirectResponse:
    return RedirectResponse(f"/org/{slug}/rfq", status_code=301)


async def _public_org_or_none(slug: str) -> tuple[dict | None, str | None]:
    """(org, None) for a live org, else (None, reason) with reason in
    {not_found, <profile_status>} for the not-available page."""
    resp = await _identity_get(f"/api/orgs/{slug}")
    if resp.status_code != 200:
        return None, "not_found"
    org = resp.json()
    if org["profile_status"] != "live":
        return None, org["profile_status"]
    return org, None


async def _profile_or_unavailable(request: Request, slug: str) -> HTMLResponse:
    org, reason = await _public_org_or_none(slug)
    if org is None:
        return templates.TemplateResponse(
            request, "profile_unavailable.html",
            {"request": request, "slug": slug, "reason": reason},
            status_code=404,
        )
    # 31f: resolve the org's UN/LOCODEs to registry names — the SAME
    # resolved `org` shape the page preview renders, so preview and
    # public page stay pixel-identical.
    org["ports"] = await resolve_ports(org.get("ports", []))
    # 32b SEO: description = the org's about flattened + truncated to
    # 160 chars (the storefront's search/social snippet).
    about = " ".join((org.get("about") or "").split())
    if len(about) > 160:
        about = about[:159].rstrip() + "…"
    meta_description = about or (
        f"Request marine survey services from {org['name']} on SeaVix."
    )
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request, "profile.html",
        {"request": request, "org": org,
         "meta_description": meta_description,
         "og_image": f"{base}{org['logo_url']}" if org.get("logo_url") else None,
         "canonical_url": f"{base}/org/{slug}"},
    )


# ---------------------------------------------------------------- profiles
# Public profiles live at /org/<slug> (owner decision, 23 Oct v4 §7).
# Root /<slug> and the legacy /p/<slug> chain 301-redirect to the canonical
# /org/<slug>; concrete app paths (/org/profile, /org/home, …) are
# registered before these and always win.

profiles_router = APIRouter()


@profiles_router.get("/org/{slug}", response_class=HTMLResponse)
@public_route
async def public_profile_org(request: Request, slug: str) -> HTMLResponse:
    return await _profile_or_unavailable(request, slug)


@profiles_router.get("/org/{slug}/rfq", response_class=HTMLResponse)
@public_route
async def rfq_form_org(request: Request, slug: str,
                       ready: str = "") -> HTMLResponse:
    org, reason = await _public_org_or_none(slug)
    if org is None:
        return templates.TemplateResponse(
            request, "profile_unavailable.html",
            {"request": request, "slug": slug, "reason": reason},
            status_code=404,
        )
    # Owner decision 24 Oct: registration happens BEFORE the form is
    # shown — the ENTRY-TIME gate. Anonymous visitors go straight to
    # register with the local `next` pointing back here (the standard
    # verify → password login → OTP chain returns them with ?ready=1);
    # only a live profile reaches this point, so draft/suspended/missing
    # keep the 404 above instead of redirecting.
    submitter = await _rfq_submitter(request)
    if submitter is None:
        return RedirectResponse(f"/register?next=/org/{slug}/rfq",
                                status_code=303)
    # The funnel returns here after verify+login (?ready=1) — the form
    # re-renders with the success flash for the signed-in user.
    return templates.TemplateResponse(request, "rfq_form.html", {
            "request": request,
            "slug": slug,
            "org_name": org["name"],
            "location_types": LOCATION_TYPES,
            "error": None,
            "ready_flash": ready == "1",
        },
    )


@profiles_router.post("/org/{slug}/rfq", response_class=HTMLResponse)
@public_route
async def rfq_submit_org(
    request: Request,
    slug: str,
    vessel_name: str = Form(""),  # validated below for a clean form error
    vessel_imo: str = Form(""),
    survey_types: list[str] = Form([]),  # 32d: catalog chip picker (multi)
    location_type: str = Form(...),
    port_name: str = Form(""),
    reference_no: str = Form(""),
    preferred_date: str = Form(""),
    scope: str = Form(""),
    contact_email: str = Form(""),  # optional — registered users are
        # identified by their org; kept for API-level back-compat
    website: str = Form(""),  # honeypot: must stay empty (real users never see it)
) -> HTMLResponse:
    client_host = request.client.host if request.client else "unknown"

    # Honeypot: bots fill hidden fields. Pretend success, persist nothing.
    if website.strip():
        return templates.TemplateResponse(request, "rfq_thanks.html",
                                          {"request": request})

    if not limiter.allow(f"rfq:{client_host}"):
        raise HTTPException(
            status_code=429, detail="too many requests — please try later"
        )

    org, _ = await _public_org_or_none(slug)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not available")

    # Entry-time gate (owner 24 Oct) backstop: an anonymous POST — a
    # crafted request reaching past the GET redirect — registers first;
    # nothing has been persisted at this point and nothing will be.
    submitter = await _rfq_submitter(request)
    if submitter is None:
        return RedirectResponse(f"/register?next=/org/{slug}/rfq",
                                status_code=303)

    def _form_error(message: str, status: int) -> HTMLResponse:
        return templates.TemplateResponse(request, "rfq_form.html", {
                "request": request,
                "slug": slug,
                "org_name": org["name"],
                "location_types": LOCATION_TYPES,
                "error": message,
            },
            status_code=status,
        )

    name = vessel_name.strip()
    if not name:
        return _form_error("vessel name is required", 400)
    imo = vessel_imo.strip()
    if imo and not re.fullmatch(r"[0-9]{7}", imo):
        return _form_error("the IMO number is 7 digits", 400)

    codes = [s.strip() for s in survey_types if s.strip()]
    if not codes:
        return _form_error("pick at least one survey type", 400)

    sid = session_id_from(request)
    work_resp = await _post_public_rfq({
        "org_slug": slug,
        "contact_email": contact_email.strip() or None,
        "vessel_name": name,
        "vessel_imo": imo or None,
        "survey_types": codes,
        "location_type": location_type,
        "port_name": port_name.strip() or None,
        "reference_no": reference_no.strip() or None,
        "preferred_date": preferred_date or None,
        "scope_notes": scope or None,
    }, headers={SESSION_HEADER: sid} if sid else None)
    if work_resp.status_code != 201:
        error = work_resp.json().get("detail", "could not submit RFQ")
        if isinstance(error, list):
            error = "; ".join(str(e.get("msg", e)) for e in error)
        return _form_error(str(error),
                           400 if work_resp.status_code in (400, 404, 422)
                           else 502)
    # 32b: confirmation + the tracking entry point (client-rail RFQs).
    return templates.TemplateResponse(request, "rfq_thanks.html",
                                      {"request": request, "track": True})


root_router = APIRouter()


@root_router.get("/{slug}", response_class=HTMLResponse)
@public_route
async def public_profile_root(slug: str) -> RedirectResponse:
    """Root /<slug> 301-redirects to the canonical /org/<slug> (v4 §7)."""
    return RedirectResponse(f"/org/{slug}", status_code=301)


@root_router.get("/{slug}/rfq", response_class=HTMLResponse)
@public_route
async def legacy_rfq_root(slug: str) -> RedirectResponse:
    return RedirectResponse(f"/org/{slug}/rfq", status_code=301)


# ---------------------------------------------------------------- dev media

@router.get("/media/{key:path}")
@public_route
async def media(key: str) -> FileResponse:
    """Dev-only: stream blobs stored by LocalBlobStore from var/blobs.

    In prod this route is replaced by a real object store + CDN signing.
    """
    root = blob_root().resolve()
    path = (root / key).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
