"""Authenticated org pages. The deny-by-default middleware guarantees a
resolved AuthContext here — require_auth reads it from the request scope
(no second identity round-trip).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from seavix_ports import AuthContext, MembershipContext, require_auth

from .auth import home_for
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
         "keepalive": _KEEPALIVE},
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


def _redirect(target: str):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(target, status_code=303)
