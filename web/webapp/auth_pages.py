"""Browser auth flows: login (password → email OTP), logout, password
reset, and the registration email-verification step.

The web app holds no credentials itself — every step proxies the identity
service, which owns Firebase verification, OTP lifecycle, and the session
table. Only the OTP-verified login response yields a session cookie (D1).
"""
from __future__ import annotations

from pathlib import Path

from seavix_ports import public_route
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    SESSION_HEADER,
    clear_session_cookie,
    home_for,
    login_redirect,
    session_context,
    session_id_from,
    set_session_cookie,
)
from .identity_client import identity_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# ------------------------------------------------------------------- login

@router.get("/login", response_class=HTMLResponse)
@public_route
async def login_form(request: Request):
    ctx = await session_context(request)
    if ctx is not None:
        kind = ctx["memberships"][0]["type"] if ctx["memberships"] else None
        return RedirectResponse(home_for(kind), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "error": None, "form": {}}
    )


@router.post("/login", response_class=HTMLResponse)
@public_route
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    resp = await identity_request(
        "POST",
        "/api/auth/login/start",
        json={"email": email.lower(), "password": password},
    )
    if resp.status_code != 200:
        error = "invalid credentials"
        if resp.status_code == 429:
            error = "too many attempts — try again later"
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": error, "form": {"email": email}},
            status_code=resp.status_code if resp.status_code == 429 else 401,
        )
    return templates.TemplateResponse(
        request,
        "login_otp.html",
        {"request": request, "email": resp.json()["email"], "error": None,
         "resent": False},
    )


@router.post("/login/otp", response_class=HTMLResponse)
@public_route
async def login_otp_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
):
    resp = await identity_request(
        "POST",
        "/api/auth/login/verify",
        json={"email": email.lower(), "code": code.strip()},
    )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "login_otp.html",
            {"request": request, "email": email.lower(), "resent": False,
             "error": "invalid or expired code"},
            status_code=401,
        )
    body = resp.json()
    kind = body["memberships"][0]["type"] if body["memberships"] else None
    target = home_for(kind)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, body["session_id"])
    return response


@router.post("/login/otp/resend", response_class=HTMLResponse)
@public_route
async def login_otp_resend(request: Request, email: str = Form(...)) -> HTMLResponse:
    """Fresh sign-in code; the identity side paces sends per email."""
    await identity_request(
        "POST", "/api/auth/login/resend", json={"email": email.lower()}
    )
    return templates.TemplateResponse(
        request, "login_otp.html",
        {"request": request, "email": email.lower(), "resent": True,
         "error": None},
    )


@router.get("/login/otp", response_class=HTMLResponse)
@public_route
async def login_otp_form(request: Request, email: str = "") -> HTMLResponse:
    """Direct GET renders the code form (e.g. back-button); the challenge
    itself only starts via POST /login."""
    return templates.TemplateResponse(
        request, "login_otp.html",
        {"request": request, "email": email, "error": None, "resent": False},
    )


# ------------------------------------------------------------------ logout

@router.post("/logout")
@public_route
async def logout(request: Request) -> RedirectResponse:
    sid = session_id_from(request)
    if sid:
        await identity_request(
            "DELETE", "/api/auth/session", headers={SESSION_HEADER: sid}
        )
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


# ------------------------------------------------------------ session ping

@router.post("/session/refresh")
@public_route
async def session_refresh(request: Request) -> dict:
    """Keep-alive: extends the sliding session.

    Contract note (D1 refresh path): when the browser Firebase SDK is
    adopted, the onIdTokenChanged handler POSTs the fresh idToken in the
    JSON body; identity verifies it against the session's uid. The plain
    cookie ping below is the SDK-free equivalent used by this scaffold.
    """
    sid = session_id_from(request)
    if not sid:
        return {"ok": False}
    resp = await identity_request(
        "POST", "/api/auth/session/refresh", json={},
        headers={SESSION_HEADER: sid},
    )
    return {"ok": resp.status_code == 200}


# ---------------------------------------------------------- password reset

@router.get("/forgot-password", response_class=HTMLResponse)
@public_route
async def forgot_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "forgot_password.html", {"request": request, "sent": False}
    )


@router.post("/forgot-password", response_class=HTMLResponse)
@public_route
async def forgot_submit(request: Request, email: str = Form(...)) -> HTMLResponse:
    await identity_request(
        "POST", "/api/auth/reset/start", json={"email": email.lower()}
    )
    # Constant response regardless of account existence (anti-enumeration).
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"request": request, "sent": True, "email": email.lower()},
    )


@router.get("/reset-password", response_class=HTMLResponse)
@public_route
async def reset_form(request: Request, oob_code: str = "") -> HTMLResponse:
    if not oob_code:
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"request": request, "error": "missing reset code", "oob_code": ""},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "reset_password.html",
        {"request": request, "oob_code": oob_code, "error": None},
    )


@router.post("/reset-password", response_class=HTMLResponse)
@public_route
async def reset_submit(
    request: Request,
    oob_code: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"request": request, "oob_code": oob_code,
             "error": "Passwords do not match."},
            status_code=400,
        )
    resp = await identity_request(
        "POST",
        "/api/auth/reset/finish",
        json={"oob_code": oob_code, "new_password": password},
    )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"request": request, "oob_code": oob_code,
             "error": "reset link is invalid or expired — request a new one"},
            status_code=400,
        )
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------- registration verification

@router.get("/register/verify/confirm", response_class=HTMLResponse)
@public_route
async def register_verify_form(request: Request, email: str = "", oob_code: str = ""):
    """Deep-link target of the verification email: confirms on POST."""
    if not oob_code:
        return templates.TemplateResponse(
            request, "verify_email_confirm.html",
            {"request": request, "email": email, "oob_code": "",
             "error": "missing verification code"},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "verify_email_confirm.html",
        {"request": request, "email": email, "oob_code": oob_code, "error": None},
    )


@router.post("/register/verify", response_class=HTMLResponse)
@public_route
async def register_verify_submit(
    request: Request,
    email: str = Form(...),
    oob_code: str = Form(...),
):
    resp = await identity_request(
        "POST",
        "/api/register/verify/confirm",
        json={"email": email.lower(), "oob_code": oob_code},
    )
    if resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "verify_email_confirm.html",
            {"request": request, "email": email.lower(), "oob_code": oob_code,
             "error": "verification link is invalid or expired — request a new one"},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "verify_email_done.html", {"request": request}
    )


@router.post("/register/verify/resend", response_class=HTMLResponse)
@public_route
async def register_verify_resend(request: Request, email: str = Form(...)) -> HTMLResponse:
    resp = await identity_request(
        "POST", "/api/register/verify/send", json={"email": email.lower()}
    )
    error = None
    resent = True
    if resp.status_code == 429:
        error = "too many requests — try again later"
        resent = False
    elif resp.status_code != 200:
        error = "could not resend the email"
        resent = False
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        {"request": request, "email": email.lower(), "error": error,
         "resent": resent},
    )
