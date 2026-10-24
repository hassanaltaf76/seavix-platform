"""Auth plumbing: bearer token → caller org via the identity service.

Work never verifies Firebase tokens itself and never reads the identity DB —
it delegates to identity's /api/internal/whoami over HTTP.
"""
from __future__ import annotations

import httpx
from fastapi import HTTPException, Request

from .config import identity_base_url

# identity's session header (services/identity/app/security.py) — the web
# app's session cookie maps to it, so it is forwarded alongside the bearer.
SESSION_HEADER = "X-Seavix-Session"


async def caller_org(request: Request) -> dict:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    async with httpx.AsyncClient(base_url=identity_base_url(), timeout=10.0) as c:
        resp = await c.get("/api/internal/whoami",
                           headers={"Authorization": header})
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="invalid token")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    return resp.json()


async def caller_member(request: Request) -> dict:
    """32a: the caller's first membership (org_id + permissions) via
    identity's auth-context endpoint. Both credential headers ride along:
    the Firebase bearer (machines/tests) and the session header (the web
    app's cookie). Every client-directory query is then scoped to
    member["org_id"] — org-scoping IS the authorization boundary."""
    headers: dict[str, str] = {}
    auth = request.headers.get("authorization", "")
    if auth:
        headers["Authorization"] = auth
    sid = request.headers.get(SESSION_HEADER, "")
    if sid:
        headers[SESSION_HEADER] = sid
    if not headers:
        raise HTTPException(status_code=401, detail="missing credentials")
    async with httpx.AsyncClient(base_url=identity_base_url(), timeout=10.0) as c:
        resp = await c.get("/api/internal/auth-context", headers=headers)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    memberships = resp.json().get("memberships") or []
    if not memberships:
        raise HTTPException(status_code=401, detail="no org membership")
    return memberships[0]


async def require_permission(request: Request, permission: str) -> dict:
    """404-over-403 on the API side too (task-23 policy): a caller whose
    membership lacks the permission gets a plain 404, never a 403."""
    member = await caller_member(request)
    if permission not in member.get("permissions", []):
        raise HTTPException(status_code=404, detail="not found")
    return member


async def resolve_public_org(org_slug: str) -> dict:
    """Identity internal org lookup — used to validate public RFQ intake."""
    async with httpx.AsyncClient(base_url=identity_base_url(), timeout=10.0) as c:
        resp = await c.get(f"/api/internal/orgs/{org_slug}")
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="organization not found")
    org = resp.json()
    if org.get("type") != "survey_company" or org.get("profile_status") != "live":
        raise HTTPException(status_code=400,
                            detail="organization is not a live survey company")
    return org
