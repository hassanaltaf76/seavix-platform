"""Web-side session handling.

The browser holds an opaque session id in the `seavix_session` HttpOnly
SameSite=Lax cookie. The deny-by-default middleware resolves every protected
request through the identity service (see WebAuthLoader); the resolved
AuthContext is available in request.scope["seavix.auth"] via require_auth.
Logout and member deactivation therefore take effect immediately.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from seavix_ports import (
    AUTH_SESSION_HEADER,
    AuthContext,
    context_from_dict,
)

from .identity_client import identity_request

SESSION_COOKIE = "seavix_session"
SESSION_HEADER = AUTH_SESSION_HEADER
SESSION_MAX_AGE = 12 * 3600  # matches identity SEAVIX_SESSION_TTL_SECONDS

# 31j: trusted-device cookie — the "remember this device for 30 days" MFA
# bypass. HttpOnly SameSite=Lax path=/; the value is the opaque token the
# identity service rotates on every trusted login. Logout keeps it.
TRUSTED_COOKIE = "seavix_trusted_device"
TRUSTED_MAX_AGE = 30 * 86400  # matches identity trusted_devices.TRUST_DAYS


class WebAuthLoader:
    """Credential -> AuthContext via the identity service's
    /api/internal/auth-context endpoint (web never touches identity's DB)."""

    async def load_from_bearer(self, token: str) -> AuthContext | None:
        return await self._fetch({"Authorization": f"Bearer {token}"})

    async def load_from_session(self, session_id: str) -> AuthContext | None:
        return await self._fetch({SESSION_HEADER: session_id})

    async def _fetch(self, headers: dict) -> AuthContext | None:
        resp = await identity_request(
            "GET", "/api/internal/auth-context", headers=headers
        )
        if resp.status_code != 200:
            return None
        return context_from_dict(resp.json())


def session_id_from(request: Request) -> str | None:
    sid = request.cookies.get(SESSION_COOKIE, "").strip()
    return sid or None


async def session_context(request: Request) -> dict | None:
    """Identity's view of the session, or None (anonymous/expired)."""
    sid = session_id_from(request)
    if not sid:
        return None
    resp = await identity_request(
        "GET", "/api/auth/session", headers={SESSION_HEADER: sid}
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def home_for(_org_type: str | None = None) -> str:
    """Post-login landing (31g landing rule): EVERY member lands on
    /dashboard — the post-auth shell any active member can count on
    (account internals are one click away at /org/home; the profile blade
    is permission-gated and would 404 for surveyor/viewer roles). The
    blade stays one click away via the rail cell, which itself only
    renders when org.profile.view holds."""
    return "/dashboard"


async def authenticated_home_redirect(request: Request) -> RedirectResponse | None:
    """Signed-out-only dual guard (31c): when a valid session cookie is
    present, return the 303 to the user's home; otherwise None so the
    auth-family handler proceeds. Applied to BOTH the GET (form render)
    and the POST (form submit) of every auth page, plus the public
    landing: an authenticated user is never dropped onto signed-out
    chrome, and no auth flow can fire while a session exists."""
    ctx = await session_context(request)
    if ctx is None:
        return None
    kind = ctx["memberships"][0]["type"] if ctx["memberships"] else None
    return RedirectResponse(home_for(kind), status_code=303)


def set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # secure=True in prod (HTTPS only); localhost dev is plain http.
        secure=False,
        path="/",
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def trusted_token_from(request: Request) -> str | None:
    token = request.cookies.get(TRUSTED_COOKIE, "").strip()
    return token or None


def set_trusted_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        TRUSTED_COOKIE,
        token,
        max_age=TRUSTED_MAX_AGE,
        httponly=True,
        samesite="lax",
        # secure=True in prod (HTTPS only); localhost dev is plain http.
        secure=False,
        path="/",
    )


def clear_trusted_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(TRUSTED_COOKIE, path="/")
