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
    authenticated_home_redirect,
    clear_session_cookie,
    clear_trusted_cookie,
    home_for,
    login_redirect,
    session_id_from,
    set_session_cookie,
    set_trusted_cookie,
    trusted_token_from,
)
from .filters import install as install_template_filters
from .identity_client import identity_request

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)


# ------------------------------------------------------------------- login

@router.get("/login", response_class=HTMLResponse)
@public_route
async def login_form(request: Request):
    # 31c signed-out-only: a live session goes straight home
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    email = email.lower()
    payload: dict = {"email": email, "password": password}
    # 31j: forward the trusted-device cookie (if any) so the identity
    # service can skip the OTP for a valid enrolment.
    trusted = trusted_token_from(request)
    if trusted:
        payload["trusted_device"] = trusted
        payload["device_user_agent"] = request.headers.get("user-agent", "")
    resp = await identity_request(
        "POST",
        "/api/auth/login/start",
        json=payload,
    )
    if resp.status_code == 403:
        # 31i: correct password, unverified email — the identity side sends
        # NO OTP and issues no challenge. The ONLY verification UI is this
        # pre-login gate: deliver a fresh link now, offer an
        # unlimited resend, and never advance toward a session.
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("code") == "email_unverified":
            sent = await identity_request(
                "POST", "/api/register/verify/send", json={"email": email}
            )
            error = None
            if sent.status_code == 429:
                error = "too many requests — try again later"
            elif sent.status_code != 200:
                error = "could not send the verification email — try again"
            return templates.TemplateResponse(
                request,
                "verify_gate.html",
                {"request": request, "email": email, "error": error,
                 "resent": False},
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
    body = resp.json()
    if body.get("session_id"):
        # 31j trusted device: factor 2 skipped, session issued directly.
        kind = body["memberships"][0]["type"] if body["memberships"] else None
        response = RedirectResponse(home_for(kind), status_code=303)
        set_session_cookie(response, body["session_id"])
        if body.get("trusted_device"):
            set_trusted_cookie(response, body["trusted_device"])
        return response
    otp_page = templates.TemplateResponse(
        request,
        "login_otp.html",
        {"request": request, "email": body["email"], "error": None,
         "resent": False},
    )
    if body.get("trusted_device_valid") is False:
        # stale/rotated-elsewhere cookie — drop it so the browser stops
        # presenting a dead token on every sign-in
        clear_trusted_cookie(otp_page)
    return otp_page


@router.post("/login/verify-email/resend", response_class=HTMLResponse)
@public_route
async def login_verify_email_resend(
    request: Request, email: str = Form(...)
) -> HTMLResponse:
    """31i: the gate screen's resend — the same identity verification-link
    pipeline (no send quota). Re-renders the gate with the
    outcome; signed-in users never see this surface."""
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    email = email.lower()
    resp = await identity_request(
        "POST", "/api/register/verify/send", json={"email": email}
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
        "verify_gate.html",
        {"request": request, "email": email, "error": error, "resent": resent},
    )


@router.post("/login/otp", response_class=HTMLResponse)
@public_route
async def login_otp_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    trust_device: str | None = Form(None),
):
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    resp = await identity_request(
        "POST",
        "/api/auth/login/verify",
        json={
            "email": email.lower(),
            "code": code.strip(),
            "trust_device": bool(trust_device),
            "device_user_agent": request.headers.get("user-agent", ""),
        },
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
    if body.get("trusted_device"):
        # 31j: the box was checked — enrol this browser for 30 days
        set_trusted_cookie(response, body["trusted_device"])
    return response


@router.post("/login/otp/resend", response_class=HTMLResponse)
@public_route
async def login_otp_resend(request: Request, email: str = Form(...)) -> HTMLResponse:
    """Fresh sign-in code; no send quota on the identity side."""
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    # 31j: logout KEEPS trusted-device enrolment — signing out of a shared
    # browser does not forfeit the 30-day MFA bypass; only revoke-all and
    # password reset do.
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request, "forgot_password.html", {"request": request, "sent": False}
    )


@router.post("/forgot-password", response_class=HTMLResponse)
@public_route
async def forgot_submit(request: Request, email: str = Form(...)) -> HTMLResponse:
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
    redirect = await authenticated_home_redirect(request)
    if redirect is not None:
        return redirect
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
