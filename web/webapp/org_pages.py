"""Authenticated org pages. The deny-by-default middleware guarantees a
resolved AuthContext here — require_auth reads it from the request scope
(no second identity round-trip).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from seavix_ports import AuthContext, MembershipContext, require_auth

from .auth import SESSION_HEADER, home_for, session_id_from
from .identity_client import identity_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_KEEPALIVE = """
<script>
setInterval(() => fetch('/session/refresh', {method: 'POST'}), 30 * 60 * 1000);
</script>
"""


def _first_membership(ctx: AuthContext) -> MembershipContext | None:
    return ctx.memberships[0] if ctx.memberships else None


async def _org_summary(slug: str) -> dict | None:
    resp = await identity_request("GET", f"/api/orgs/{slug}")
    return resp.json() if resp.status_code == 200 else None


def _redirect(target: str):
    return RedirectResponse(target, status_code=303)


@router.get("/org/profile", response_class=HTMLResponse)
async def org_profile(request: Request, ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect(home_for(membership.org_type))
    org = await _org_summary(membership.slug)
    return templates.TemplateResponse(
        request,
        "org_profile.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE, "slug_saved": False, "slug_error": None,
         "logo_saved": False, "logo_error": None},
    )


@router.post("/org/profile", response_class=HTMLResponse)
async def org_profile_update(
    request: Request,
    slug: str = Form(...),
    about: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    """Public page editor: services description + page slug. Proxies the
    identity profile update (org.profile.edit permission enforced there);
    re-renders inline with the field-level error on conflict."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    sid = session_id_from(request)
    resp = await identity_request(
        "PUT", "/api/orgs/me/profile",
        json={"slug": slug.strip(), "about": about.strip() or None},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code == 200:
        new_slug = resp.json().get("slug", membership.slug)
        org = await _org_summary(new_slug)
        membership = _refresh_membership(ctx, new_slug)
        return templates.TemplateResponse(
            request,
            "org_profile.html",
            {"request": request, "ctx": ctx, "membership": membership,
             "org": org, "keepalive": _KEEPALIVE, "slug_saved": True,
             "slug_error": None, "logo_saved": False, "logo_error": None},
        )
    slug_error = "could not save — try again"
    if resp.status_code == 409:
        detail = resp.json().get("detail", {})
        slug_error = f"{detail.get('field', 'slug')}: {detail.get('message', 'conflict')}"
    elif resp.status_code == 422:
        detail = resp.json().get("detail", [])
        msg = ""
        if isinstance(detail, list) and detail:
            msg = str(detail[0].get("msg", ""))
        slug_error = ("that address is reserved — pick another"
                      if "reserved" in msg else
                      "use 2-64 lowercase letters, digits and dashes")
    org = await _org_summary(membership.slug)
    return templates.TemplateResponse(
        request,
        "org_profile.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE, "slug_saved": False,
         "slug_error": slug_error, "logo_saved": False, "logo_error": None},
        status_code=resp.status_code if resp.status_code in (409, 422) else 502,
    )


@router.post("/org/profile/logo", response_class=HTMLResponse)
async def org_profile_logo(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_auth),
):
    """Logo upload proxies to the identity service (org.logo.upload enforced
    there); re-renders the editor with the outcome."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/orgs/me/logo",
        files={"file": (file.filename or "logo",
                        await file.read(), file.content_type)},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    org = await _org_summary(membership.slug)
    logo_error = None
    if resp.status_code != 200:
        logo_error = resp.json().get("detail", "could not upload — try again")
    return templates.TemplateResponse(
        request,
        "org_profile.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE, "slug_saved": False, "slug_error": None,
         "logo_saved": resp.status_code == 200, "logo_error": logo_error},
        status_code=200 if resp.status_code == 200 else 502,
    )


def _refresh_membership(ctx: AuthContext, slug: str) -> MembershipContext:
    """MembershipContext is frozen; rebuild with the fresh slug."""
    m = ctx.memberships[0]
    return MembershipContext(
        org_id=m.org_id,
        slug=slug,
        org_type=m.org_type,
        roles=m.roles,
        permissions=m.permissions,
    )


@router.get("/org/home", response_class=HTMLResponse)
async def org_home(request: Request, ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    org = await _org_summary(membership.slug) if membership else None
    return templates.TemplateResponse(
        request,
        "org_home.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE},
    )
