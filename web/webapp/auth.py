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


def home_for(org_type: str | None) -> str:
    """Post-login landing, by org type (single-org scaffold: the first
    membership decides)."""
    if org_type == "survey_company":
        return "/org/profile"
    return "/org/home"


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
