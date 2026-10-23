"""Deny-by-default sweep — zero accidental public endpoints.

For both apps (identity + web):
1. The AuthMiddleware is installed app-wide.
2. EVERY HTTP route is accounted for: public-marked routes must be in the
   explicit public allowlist; everything else must be in the protected
   inventory. A route that is marked public but not allowlisted (or vice
   versa) fails the suite — this is what makes "deny by default" auditable.
3. Functional probes: protected routes reject unauthenticated and garbage
   credentials (401 JSON on identity, 303 → /login on web); public routes
   stay reachable without credentials.

Static mounts are infrastructure and always public by design.
"""
import os
import sys
import uuid

import httpx
import pytest
from starlette.routing import Mount

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "services", "identity")
    ),
)

from app.main import app as identity_app  # noqa: E402
from webapp.main import app as web_app  # noqa: E402
from seavix_ports import AuthMiddleware, is_public, iter_leaf_routes  # noqa: E402

IDENTITY_PUBLIC = {
    "/healthz",
    "/api/register",
    "/api/register/verify/send",
    "/api/register/verify/confirm",
    "/api/auth/login/start",
    "/api/auth/login/verify",
    "/api/auth/login/resend",
    "/api/auth/reset/start",
    "/api/auth/reset/finish",
    "/api/orgs/{slug}",
    "/api/internal/orgs/{slug}",
    # invite acceptance (the invitee has no credentials yet); guarded by the
    # single-use hashed token, not by the wall
    "/api/public/invites/accept",
    # OIDC relying-party callback (D3): the caller is an external IdP's
    # user with a fresh token, not a wall credential
    "/api/public/auth/oidc/callback",
    # Dev-only peeks; inert (404) unless SEAVIX_DEV_OTP_ENDPOINT=1. The
    # flag is the gate, and it never ships enabled.
    "/api/dev/last-otp",
    "/api/dev/last-invite",
}

IDENTITY_PROTECTED = {
    "/api/orgs/me/profile",
    "/api/orgs/me/logo",
    "/api/orgs/me/profile-status",
    "/api/orgs/me/roles",
    "/api/orgs/me/roles/{role_id}",
    "/api/orgs/me/members",
    "/api/orgs/me/members/{user_id}/roles",
    "/api/orgs/me/members/{user_id}/deactivate",
    "/api/orgs/me/members/{user_id}/reactivate",
    "/api/orgs/me/invites",
    "/api/orgs/me/idps",
    "/api/orgs/me/idps/{idp_id}",
    "/api/auth/session",
    "/api/auth/session/refresh",
    # 31j: trusted-device account surface (list + revoke-all)
    "/api/auth/trusted-devices",
    "/api/internal/whoami",
    "/api/internal/auth-context",
}

WEB_PUBLIC = {
    "/healthz",
    "/",
    "/register",
    "/p/{slug}",
    "/p/{slug}/rfq",
    # v4 §7: canonical public profiles at /org/<slug>
    "/org/{slug}",
    "/org/{slug}/rfq",
    # root /<slug> and /<slug>/rfq are 301 redirects to /org/<slug>
    "/{slug}",
    "/{slug}/rfq",
    "/media/{key:path}",
    "/login",
    "/login/otp",
    "/login/otp/resend",
    # 31i: the pre-login gate's resend (proxies the verification-link send)
    "/login/verify-email/resend",
    "/logout",
    "/forgot-password",
    "/reset-password",
    "/register/verify",
    "/register/verify/confirm",
    "/register/verify/resend",
    # no-op without a valid cookie (identity 401 -> {"ok": false})
    "/session/refresh",
}

WEB_PROTECTED = {
    "/org/profile",
    "/org/profile/logo",
    "/org/home",
    # 31g: the post-auth landing blade
    "/dashboard",
    # 31f: port-picker combobox backend (proxies the registry)
    "/org/ports/search",
    # 31j: 'Sign out all trusted devices' (account surface action)
    "/account/trusted-devices/revoke-all",
}


def _inventory(app, public_set, protected_set, label):
    assert any(
        m.cls is AuthMiddleware for m in app.user_middleware
    ), f"{label}: AuthMiddleware not installed"

    seen = set()
    for route in iter_leaf_routes(app.routes):
        if isinstance(route, Mount):
            continue
        path = route.path
        seen.add(path)
        endpoint = route.endpoint
        if is_public(endpoint):
            assert path in public_set, (
                f"{label}: {path} is marked PUBLIC but not in the allowlist "
                f"— either un-mark it or consciously extend the allowlist"
            )
        else:
            assert path in protected_set, (
                f"{label}: {path} is unmarked (protected by the wall) but "
                f"absent from the protected inventory — record it"
            )

    missing = (public_set | protected_set) - seen
    assert not missing, f"{label}: allowlist/inventory paths with no route: {missing}"


def test_identity_route_inventory():
    _inventory(identity_app, IDENTITY_PUBLIC, IDENTITY_PROTECTED, "identity")


def test_web_route_inventory():
    _inventory(web_app, WEB_PUBLIC, WEB_PROTECTED, "web")


def test_identity_wall_functional(arun):
    async def scenario():
        u = uuid.uuid4().hex[:8]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=identity_app), base_url="http://t"
        ) as c:
            # protected, no credentials
            r = await c.get("/api/auth/session")
            assert r.status_code == 401
            assert r.json() == {"detail": "authentication required"}
            # protected, garbage credentials
            g = await c.get(
                "/api/auth/session",
                headers={"X-Seavix-Session": str(uuid.uuid4())},
            )
            assert g.status_code == 401
            b = await c.get(
                "/api/orgs/me/profile",
                headers={"Authorization": "Bearer garbage"},
            )
            assert b.status_code == 401
            # unknown path without credentials -> 401, never an unauth'd 404
            n = await c.get(f"/api/nope-{u}")
            assert n.status_code == 401
            # public routes still open
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.post(
                "/api/auth/login/start",
                json={"email": f"x-{u}@example.com", "password": "secret-123"},
            )).status_code == 401  # endpoint-level denial, still no wall leak

    arun(scenario())


def test_web_wall_functional(arun):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://t"
        ) as c:
            r = await c.get("/org/profile", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/login"
            # garbage cookie -> same treatment (fail closed)
            c.cookies.set("seavix_session", str(uuid.uuid4()))
            g = await c.get("/org/home", follow_redirects=False)
            assert g.status_code == 303
            # public pages stay reachable
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/login")).status_code == 200

    arun(scenario())
