"""Auth plumbing: bearer token / session → caller org via the identity
service.

Work never verifies Firebase tokens itself and never reads the identity DB —
it delegates to identity's /api/internal/* over HTTP.

Test seam (same pattern as webapp.identity_client): pytest swaps the
transport for an in-process ASGI transport; None in production = real HTTP
to identity_base_url().
"""
from __future__ import annotations

import httpx
from fastapi import HTTPException, Request

from .config import identity_base_url

# identity's session header (services/identity/app/security.py) — the web
# app's session cookie maps to it, so it is forwarded alongside the bearer.
SESSION_HEADER = "X-Seavix-Session"

_identity_transport: httpx.AsyncBaseTransport | None = None


def set_identity_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _identity_transport
    _identity_transport = transport


def identity_transport() -> httpx.AsyncBaseTransport | None:
    return _identity_transport


async def caller_org(request: Request) -> dict:
    """The caller's org — via identity's auth-context so BOTH credential
    kinds work: machine clients present a Firebase bearer, the web app's
    session cookie rides X-Seavix-Session. (32b: the RFQ spine is now
    reachable from the browser, not only from bearer-token API clients.)
    The returned dict also carries the top-level uid/email when identity
    includes them (32c: quote documents stamp the caller)."""
    headers: dict[str, str] = {}
    auth = request.headers.get("authorization", "")
    if auth:
        headers["Authorization"] = auth
    sid = request.headers.get(SESSION_HEADER, "")
    if sid:
        headers[SESSION_HEADER] = sid
    if not headers:
        raise HTTPException(status_code=401, detail="missing credentials")
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=_identity_transport
    ) as c:
        resp = await c.get("/api/internal/auth-context", headers=headers)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    ctx = resp.json()
    memberships = ctx.get("memberships") or []
    if not memberships:
        raise HTTPException(status_code=401, detail="no org membership")
    m = memberships[0]
    out = {"org_id": m["org_id"], "slug": m["slug"], "type": m["org_type"]}
    if ctx.get("uid") is not None:
        out["uid"] = ctx["uid"]
    if ctx.get("email") is not None:
        out["email"] = ctx["email"]
    return out


async def caller_member(request: Request) -> dict:
    """32a: the caller's first membership (org_id + permissions) via
    identity's auth-context endpoint. Both credential headers ride along:
    the Firebase bearer (machines/tests) and the session header (the web
    app's cookie). Every client-directory query is then scoped to
    member["org_id"] — org-scoping IS the authorization boundary.
    32c: the membership dict also carries the auth-context's TOP-LEVEL
    uid/email when present (quote documents stamp prepared_by/approved_by);
    absent from the identity payload means absent here — callers must use
    member.get("uid")."""
    headers: dict[str, str] = {}
    auth = request.headers.get("authorization", "")
    if auth:
        headers["Authorization"] = auth
    sid = request.headers.get(SESSION_HEADER, "")
    if sid:
        headers[SESSION_HEADER] = sid
    if not headers:
        raise HTTPException(status_code=401, detail="missing credentials")
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=_identity_transport
    ) as c:
        resp = await c.get("/api/internal/auth-context", headers=headers)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity service error")
    ctx = resp.json()
    memberships = ctx.get("memberships") or []
    if not memberships:
        raise HTTPException(status_code=401, detail="no org membership")
    member = dict(memberships[0])
    if ctx.get("uid") is not None:
        member["uid"] = ctx["uid"]
    if ctx.get("email") is not None:
        member["email"] = ctx["email"]
    return member


async def require_permission(request: Request, permission: str) -> dict:
    """404-over-403 on the API side too (task-23 policy): a caller whose
    membership lacks the permission gets a plain 404, never a 403."""
    member = await caller_member(request)
    if permission not in member.get("permissions", []):
        raise HTTPException(status_code=404, detail="not found")
    return member


async def resolve_public_org(org_slug: str) -> dict:
    """Identity internal org lookup — used to validate public RFQ intake."""
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=_identity_transport
    ) as c:
        resp = await c.get(f"/api/internal/orgs/{org_slug}")
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="organization not found")
    org = resp.json()
    if org.get("type") != "survey_company" or org.get("profile_status") != "live":
        raise HTTPException(status_code=400,
                            detail="organization is not a live survey company")
    return org
