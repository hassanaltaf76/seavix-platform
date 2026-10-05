"""Auth plumbing: bearer token → caller org via the identity service.

Work never verifies Firebase tokens itself and never reads the identity DB —
it delegates to identity's /api/internal/whoami over HTTP.
"""
from __future__ import annotations

import httpx
from fastapi import HTTPException, Request

from .config import identity_base_url


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
