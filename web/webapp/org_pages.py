"""Authenticated org pages (session-cookie only — deny-by-default arrives
with the task-21 middleware; these routes already require a live session).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import home_for, login_redirect, session_context
from .identity_client import identity_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_KEEPALIVE = """
<script>
setInterval(() => fetch('/session/refresh', {method: 'POST'}), 30 * 60 * 1000);
</script>
"""


def _first_membership(ctx: dict) -> dict | None:
    return ctx["memberships"][0] if ctx["memberships"] else None


async def _org_summary(slug: str) -> dict | None:
    resp = await identity_request("GET", f"/api/orgs/{slug}")
    return resp.json() if resp.status_code == 200 else None


@router.get("/org/profile", response_class=HTMLResponse)
async def org_profile(request: Request):
    ctx = await session_context(request)
    if ctx is None:
        return login_redirect()
    membership = _first_membership(ctx)
    if membership is None:
        return RedirectResponse("/org/home", status_code=303)
    if membership["type"] != "survey_company":
        return RedirectResponse(home_for(membership["type"]), status_code=303)
    org = await _org_summary(membership["slug"])
    return templates.TemplateResponse(
        request,
        "org_profile.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE},
    )


@router.get("/org/home", response_class=HTMLResponse)
async def org_home(request: Request):
    ctx = await session_context(request)
    if ctx is None:
        return login_redirect()
    membership = _first_membership(ctx)
    org = await _org_summary(membership["slug"]) if membership else None
    return templates.TemplateResponse(
        request,
        "org_home.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "keepalive": _KEEPALIVE},
    )
