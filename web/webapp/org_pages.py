"""Authenticated org pages. The deny-by-default middleware guarantees a
resolved AuthContext here — require_auth reads it from the request scope
(no second identity round-trip).

31f: the profile blade is a VIEW/EDIT pair. View mode renders the org's
public page through the SAME content partial as /<slug> (WYSIWYG by
construction); edit mode (org.profile.edit) carries the Save button that
persists about + slug + ports of operation in one PUT.

31g: /dashboard is the post-auth landing blade (home_for).

31i: login gates on a verified email, so every post-auth surface carries
ZERO verification state — no banner, no account-menu tag, no resend. The
only verification UI is the pre-login gate screen (auth_pages)."""
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
from .filters import install as install_template_filters
from .identity_client import identity_request
from .registry_client import registry_request, resolve_ports

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

_KEEPALIVE = """
<script>
setInterval(() => fetch('/session/refresh', {method: 'POST'}), 30 * 60 * 1000);
</script>
"""

ORG_PROFILE_VIEW = "org.profile.view"
ORG_PROFILE_EDIT = "org.profile.edit"
ORG_LOGO_UPLOAD = "org.logo.upload"


def _first_membership(ctx: AuthContext) -> MembershipContext | None:
    return ctx.memberships[0] if ctx.memberships else None


async def _org_summary(slug: str) -> dict | None:
    resp = await identity_request("GET", f"/api/orgs/{slug}")
    return resp.json() if resp.status_code == 200 else None


async def _org_with_ports(org: dict | None) -> dict | None:
    """Public org summary with ports resolved to registry names — the one
    shape the shared content partial renders (31f)."""
    if org is None:
        return None
    resolved = await resolve_ports(org.get("ports") or [])
    return {**org, "ports": resolved}


def _redirect(target: str):
    return RedirectResponse(target, status_code=303)


def _has(membership: MembershipContext | None, permission: str) -> bool:
    return membership is not None and permission in membership.permissions


def _rail_org(org: dict | None, membership: MembershipContext | None) -> dict:
    """31g: the rail company cell renders on every blade — the org logo
    when one is set, otherwise initials derived from the company name,
    plus the name itself. Same public org summary the blade renders."""
    name = (org or {}).get("name") or (membership.slug if membership else "")
    words = name.split()
    initials = ("".join(w[0] for w in words[:2]) or "S").upper()
    return {"name": name, "logo_url": (org or {}).get("logo_url"),
            "initials": initials}


def _blade_not_found(request: Request, ctx: AuthContext) -> HTMLResponse:
    """404-over-403 (task-23 policy, extended to permission-gated blades):
    a surface the membership has no permission to know about renders the
    friendly not-found blade with a REAL 404 — never a 403."""
    membership = _first_membership(ctx)
    return templates.TemplateResponse(
        request,
        "org_not_found.html",
        {"request": request, "ctx": ctx, "membership": membership,
         "rail_org": _rail_org(None, membership), "keepalive": _KEEPALIVE},
        status_code=404,
    )


def _view_context(ctx, membership, org, saved: bool) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "keepalive": _KEEPALIVE, "mode": "view",
        "can_edit": _has(membership, ORG_PROFILE_EDIT), "saved": saved,
    }


def _edit_context(ctx, membership, org, *, slug_error=None, port_error=None,
                  logo_saved=False, logo_error=None) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "keepalive": _KEEPALIVE, "mode": "edit",
        "can_edit": _has(membership, ORG_PROFILE_EDIT),
        "slug_error": slug_error, "port_error": port_error,
        "logo_saved": logo_saved, "logo_error": logo_error,
    }


def _render(request: Request, template: str, ctx: dict,
            status_code: int = 200) -> HTMLResponse:
    ctx["request"] = request
    return templates.TemplateResponse(request, template, ctx,
                                      status_code=status_code)


@router.get("/org/profile", response_class=HTMLResponse)
async def org_profile(
    request: Request,
    view: str = "",
    saved: str = "",
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect(home_for(membership.org_type))
    # 31e: the blade requires org.profile.view. Without it this route is a
    # 404 (not 403), and the rail company cell that links here is hidden.
    if not _has(membership, ORG_PROFILE_VIEW):
        return _blade_not_found(request, ctx)
    org = await _org_with_ports(await _org_summary(membership.slug))
    # 31f view/edit split: ?view=edit opens the editor (edit permission
    # required); anything else renders the WYSIWYG public preview.
    if view == "edit" and _has(membership, ORG_PROFILE_EDIT):
        return _render(request, "org_profile.html",
                       _edit_context(ctx, membership, org))
    return _render(request, "org_profile.html",
                   _view_context(ctx, membership, org, saved == "1"))


@router.get("/org/ports/search")
async def org_ports_search(request: Request, q: str = "",
                           ctx: AuthContext = Depends(require_auth)):
    """Alpine combobox backend for the port picker: proxies the registry
    port search (name / UN/LOCODE / country). Reference data — any signed-in
    member may query it; only edit mode surfaces it."""
    resp = await registry_request("GET", "/ports", params={"search": q})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="registry unavailable")
    return resp.json()


@router.post("/org/profile")
async def org_profile_update(
    request: Request,
    slug: str = Form(...),
    about: str = Form(""),
    ports: list[str] = Form([]),
    ctx: AuthContext = Depends(require_auth),
):
    """Edit-mode Save: services description + page slug + ports of
    operation in one PUT. Proxies the identity profile update; unknown
    port codes are rejected against the registry before any write; field
    errors re-render edit mode inline. 31e: edits require
    org.profile.edit — otherwise the same 404 as the blade."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    if not _has(membership, ORG_PROFILE_EDIT):
        return _blade_not_found(request, ctx)

    codes = [p.strip().upper() for p in ports if p.strip()]
    org = await _org_with_ports(await _org_summary(membership.slug))
    resolved = await resolve_ports(codes)
    known = {p["code"] for p in resolved}
    unknown = [c for c in codes if c not in known]
    if unknown:
        return _render(
            request, "org_profile.html",
            {**_edit_context(ctx, membership, org,
                             port_error=f"unknown port: {unknown[0]}"),
             "org": {**(org or {}), "ports": resolved}},
            status_code=422,
        )

    sid = session_id_from(request)
    resp = await identity_request(
        "PUT", "/api/orgs/me/profile",
        json={"slug": slug.strip(), "about": about.strip() or None,
              "ports": codes},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code == 200:
        return _redirect("/org/profile?saved=1")
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
    return _render(
        request, "org_profile.html",
        {**_edit_context(ctx, membership, org, slug_error=slug_error),
         "org": {**(org or {}), "ports": resolved}},
        status_code=resp.status_code if resp.status_code in (409, 422) else 502,
    )


@router.post("/org/profile/logo", response_class=HTMLResponse)
async def org_profile_logo(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_auth),
):
    """Logo upload proxies to the identity service (org.logo.upload enforced
    there); re-renders EDIT mode with the outcome. 31e: the route itself
    pre-checks org.logo.upload — without it, the same 404 as the blade."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    if not _has(membership, ORG_LOGO_UPLOAD):
        return _blade_not_found(request, ctx)
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/orgs/me/logo",
        files={"file": (file.filename or "logo",
                        await file.read(), file.content_type)},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    org = await _org_with_ports(await _org_summary(membership.slug))
    logo_error = None
    if resp.status_code != 200:
        logo_error = resp.json().get("detail", "could not upload — try again")
    return _render(
        request, "org_profile.html",
        _edit_context(ctx, membership, org,
                      logo_saved=resp.status_code == 200, logo_error=logo_error),
        status_code=200 if resp.status_code == 200 else 502,
    )


@router.get("/org/home", response_class=HTMLResponse)
async def org_home(request: Request, ctx: AuthContext = Depends(require_auth)):
    membership = _first_membership(ctx)
    org = await _org_summary(membership.slug) if membership else None
    return templates.TemplateResponse(
        request,
        "org_home.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "rail_org": _rail_org(org, membership),
         "keepalive": _KEEPALIVE},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request,
                    ctx: AuthContext = Depends(require_auth)):
    """31g: the post-auth landing blade — every active member lands here
    after sign-in (home_for), and signed-in "/" redirects here. Until the
    operational surfaces (jobs, RFQs, activity) ship, the body is an
    honest placeholder. 31i: zero verification state — login gates on a
    verified email, so post-auth chrome carries no banner, tag or resend."""
    membership = _first_membership(ctx)
    org = await _org_summary(membership.slug) if membership else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "rail_org": _rail_org(org, membership),
         "keepalive": _KEEPALIVE},
    )
