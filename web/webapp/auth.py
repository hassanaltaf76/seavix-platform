"""Web-side session handling.

The browser holds an opaque session id in the `seavix_session` HttpOnly
SameSite=Lax cookie. User context is resolved per request from the identity
service (which validates against its server-side session table), so logout
and member deactivation take effect immediately.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from .identity_client import identity_request

SESSION_COOKIE = "seavix_session"
SESSION_HEADER = "X-Seavix-Session"
SESSION_MAX_AGE = 12 * 3600  # matches identity SEAVIX_SESSION_TTL_SECONDS


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


def home_for(account_kind: str | None) -> str:
    """Post-login landing, by account kind (single-org scaffold: the first
    membership decides)."""
    if account_kind == "survey_company":
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
