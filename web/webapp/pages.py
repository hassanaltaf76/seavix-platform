"""Public pages: landing, registration, org profile, RFQ intake, dev media."""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import blob_root, identity_base_url, work_base_url
from .ratelimit import RateLimiter

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
limiter = RateLimiter(limit=5, window_seconds=3600)

SURVEY_TYPES = ("pre-purchase", "condition", "bunker", "loading-discharging")
LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth", "dry-dock")


async def _identity_get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(base_url=identity_base_url(), timeout=10.0) as c:
        return await c.get(path)


async def _post_public_rfq(payload: dict) -> httpx.Response:
    """RFQ intake lives in the work service (spine). Scaffold-grade direct
    HTTP call; a service-mesh/queue belongs to a later slice."""
    async with httpx.AsyncClient(base_url=work_base_url(), timeout=10.0) as c:
        return await c.post("/api/public/rfqs", json=payload)


# ---------------------------------------------------------------- landing

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html", {"request": request})


# ---------------------------------------------------------------- register

@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "register.html", {"request": request, "error": None, "form": {}}
    )


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    org_name: str = Form(...),
    org_type: str = Form(...),
    slug: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    account_kind: str = Form(...),
) -> HTMLResponse:
    form = {
        "org_name": org_name,
        "org_type": org_type,
        "slug": slug,
        "email": email,
        "account_kind": account_kind,
    }
    async with httpx.AsyncClient(base_url=identity_base_url(), timeout=10.0) as c:
        resp = await c.post("/api/register", json={**form, "password": password})
    if resp.status_code == 201:
        return templates.TemplateResponse(request, "register_success.html", {"request": request, "slug": slug, "account_kind": account_kind},
        )
    if resp.status_code == 409:
        detail = resp.json().get("detail", {})
        msg = f"{detail.get('field', 'field')}: {detail.get('message', 'conflict')}"
        return templates.TemplateResponse(request, "register.html", {"request": request, "error": msg, "form": form},
            status_code=409,
        )
    raise HTTPException(status_code=502, detail="registration service error")


# ---------------------------------------------------------------- profiles

@router.get("/p/{slug}", response_class=HTMLResponse)
async def public_profile(request: Request, slug: str) -> HTMLResponse:
    resp = await _identity_get(f"/api/orgs/{slug}")
    if resp.status_code != 200:
        return templates.TemplateResponse(request, "profile_unavailable.html", {"request": request, "slug": slug, "reason": "not_found"},
            status_code=404,
        )
    org = resp.json()
    if org["profile_status"] != "live":
        return templates.TemplateResponse(request, "profile_unavailable.html", {"request": request, "slug": slug, "reason": org["profile_status"]},
            status_code=404,
        )
    return templates.TemplateResponse(request, "profile.html", {"request": request, "org": org})


# ---------------------------------------------------------------- RFQ

@router.get("/p/{slug}/rfq", response_class=HTMLResponse)
async def rfq_form(request: Request, slug: str) -> HTMLResponse:
    resp = await _identity_get(f"/api/orgs/{slug}")
    if resp.status_code != 200 or resp.json()["profile_status"] != "live":
        return templates.TemplateResponse(request, "profile_unavailable.html", {"request": request, "slug": slug, "reason": "unavailable"},
            status_code=404,
        )
    return templates.TemplateResponse(request, "rfq_form.html", {
            "request": request,
            "slug": slug,
            "org_name": resp.json()["name"],
            "survey_types": SURVEY_TYPES,
            "location_types": LOCATION_TYPES,
            "error": None,
        },
    )


@router.post("/p/{slug}/rfq", response_class=HTMLResponse)
async def rfq_submit(
    request: Request,
    slug: str,
    vessel_imo: str = Form(...),
    survey_type: str = Form(...),
    location_type: str = Form(...),
    contact_email: str = Form(""),  # validated below for a clean form error
    preferred_date: str = Form(""),
    scope: str = Form(""),
    website: str = Form(""),  # honeypot: must stay empty (real users never see it)
) -> HTMLResponse:
    client_host = request.client.host if request.client else "unknown"

    # Honeypot: bots fill hidden fields. Pretend success, persist nothing.
    if website.strip():
        return templates.TemplateResponse(request, "rfq_thanks.html", {"request": request})

    if not limiter.allow(f"rfq:{client_host}"):
        raise HTTPException(
            status_code=429, detail="too many requests — please try later"
        )

    resp = await _identity_get(f"/api/orgs/{slug}")
    if resp.status_code != 200 or resp.json()["profile_status"] != "live":
        raise HTTPException(status_code=404, detail="organization not available")

    if not contact_email.strip():
        return templates.TemplateResponse(request, "rfq_form.html", {
                "request": request,
                "slug": slug,
                "org_name": resp.json()["name"],
                "survey_types": SURVEY_TYPES,
                "location_types": LOCATION_TYPES,
                "error": "contact email is required",
            },
            status_code=400,
        )

    work_resp = await _post_public_rfq({
        "org_slug": slug,
        "contact_email": contact_email.strip(),
        "vessel_imo": vessel_imo.strip() or None,
        "survey_type": survey_type,
        "location_type": location_type,
        "preferred_date": preferred_date or None,
        "scope_notes": scope or None,
    })
    if work_resp.status_code != 201:
        error = work_resp.json().get("detail", "could not submit RFQ")
        if isinstance(error, list):
            error = "; ".join(str(e.get("msg", e)) for e in error)
        return templates.TemplateResponse(request, "rfq_form.html", {
                "request": request,
                "slug": slug,
                "org_name": resp.json()["name"],
                "survey_types": SURVEY_TYPES,
                "location_types": LOCATION_TYPES,
                "error": str(error),
            },
            status_code=400 if work_resp.status_code in (400, 404, 422) else 502,
        )
    return templates.TemplateResponse(request, "rfq_thanks.html", {"request": request})


# ---------------------------------------------------------------- dev media

@router.get("/media/{key:path}")
async def media(key: str) -> FileResponse:
    """Dev-only: stream blobs stored by LocalBlobStore from var/blobs.

    In prod this route is replaced by a real object store + CDN signing.
    """
    root = blob_root().resolve()
    path = (root / key).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
