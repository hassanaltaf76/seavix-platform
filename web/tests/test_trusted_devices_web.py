"""31j browser-level trusted-device MFA tests.

The whole loop through the web app + identity service in-process:
- the OTP challenge carries the "Trust this device for 30 days" checkbox
- checking it sets the trusted cookie (HttpOnly, SameSite=Lax, path /)
- trust -> logout -> login with the password alone skips the OTP
- a fresh/incognito browser (no cookie) always gets the OTP
- revoke-all from the account surface kills every enrolment AND the
  local cookie, so the next login needs the OTP again
- an unchecked challenge sets no cookie: OTP every login
"""
import re
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE, TRUSTED_COOKIE


def _cookie(response: httpx.Response, name: str) -> str | None:
    set_cookie = response.headers.get("set-cookie", "")
    match = re.search(rf"{name}=([^;]+)", set_cookie)
    return match.group(1) if match else None


def _cookie_attrs(response: httpx.Response, name: str) -> str:
    set_cookie = response.headers.get("set-cookie", "")
    match = re.search(rf"{name}=[^;]+;(.+)", set_cookie)
    return match.group(1).lower() if match else ""


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _register_and_verify(idc: httpx.AsyncClient, email: str) -> None:
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "Web Trusted Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _otp_code(idc: httpx.AsyncClient, email: str) -> str:
    resp = await idc.get("/api/dev/last-otp", params={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


def test_trust_checkbox_sets_cookie_and_skips_otp_next_login(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wt-{uuid.uuid4().hex[:8]}@example.com"
            await _register_and_verify(idc, email)

            async with _web() as w:
                # 1. password-only login on a NEW browser -> OTP page with
                #    the trust checkbox, no trusted cookie
                start = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert start.status_code == 200
                assert "Trust this device for 30 days" in start.text
                assert TRUSTED_COOKIE not in start.headers.get("set-cookie", "")

                # 2. OTP with the box checked -> session + trusted cookies
                code = await _otp_code(idc, email)
                ok = await w.post(
                    "/login/otp",
                    data={"email": email, "code": code, "trust_device": "1"},
                )
                assert ok.status_code == 303
                assert _cookie(ok, SESSION_COOKIE)
                trusted = _cookie(ok, TRUSTED_COOKIE)
                assert trusted and "." in trusted
                attrs = _cookie_attrs(ok, TRUSTED_COOKIE)
                assert "httponly" in attrs
                assert "samesite=lax" in attrs
                assert "path=/" in attrs
                assert "max-age=2592000" in attrs  # 30 days

                # 3. logout KEEPS the trusted cookie
                out = await w.post(
                    "/logout",
                    cookies={TRUSTED_COOKIE: trusted},
                )
                assert out.status_code == 303
                assert _cookie(out, TRUSTED_COOKIE) is None  # not rewritten

                # 4. password-only login WITH the trusted cookie -> the OTP
                #    is skipped entirely (303 straight home)
                again = await w.post(
                    "/login",
                    data={"email": email, "password": "secret-123"},
                    cookies={TRUSTED_COOKIE: trusted},
                )
                assert again.status_code == 303
                assert again.headers["location"] == "/dashboard"
                assert _cookie(again, SESSION_COOKIE)

    arun(scenario())


def test_incognito_and_unchecked_logins_always_need_otp(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wt-{uuid.uuid4().hex[:8]}@example.com"
            await _register_and_verify(idc, email)

            async with _web() as w:
                # unchecked challenge: NO trusted cookie, and the NEXT login
                # needs the OTP again (OTP fatigue class fixed by the box,
                # never by silent trust)
                start = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                code = await _otp_code(idc, email)
                ok = await w.post(
                    "/login/otp", data={"email": email, "code": code}
                )
                assert ok.status_code == 303
                assert TRUSTED_COOKIE not in ok.headers.get("set-cookie", "")

                out = await w.post("/logout")
                assert out.status_code == 303

                # incognito: no cookie -> OTP page again
                again = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert again.status_code == 200
                assert "Check your inbox" in again.text

    arun(scenario())


def test_revoke_all_from_account_surface(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wt-{uuid.uuid4().hex[:8]}@example.com"
            await _register_and_verify(idc, email)

            async with _web() as w:
                await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                code = await _otp_code(idc, email)
                ok = await w.post(
                    "/login/otp",
                    data={"email": email, "code": code, "trust_device": "1"},
                )
                trusted = _cookie(ok, TRUSTED_COOKIE)
                sid = _cookie(ok, SESSION_COOKIE)
                jar = {SESSION_COOKIE: sid, TRUSTED_COOKIE: trusted}

                # account surface shows the device and the action
                home = await w.get("/org/home", cookies=jar)
                assert home.status_code == 200
                assert "1 trusted device" in home.text
                assert "Sign out all trusted devices" in home.text

                # revoke-all: identity rows revoked + local cookie dropped
                revoked = await w.post(
                    "/account/trusted-devices/revoke-all", cookies=jar
                )
                assert revoked.status_code == 303
                assert revoked.headers["location"] == "/org/home?devices=revoked"
                assert 'seavix_trusted_device="";' in (
                    revoked.headers.get("set-cookie", "").lower()
                )

                # count is zero, and the next password-only login needs OTP
                home2 = await w.get("/org/home", cookies=jar)
                assert "No trusted devices" in home2.text

                # fresh browser presenting the revoked cookie: OTP again
                async with _web() as w2:
                    login = await w2.post(
                        "/login",
                        data={"email": email, "password": "secret-123"},
                        cookies={TRUSTED_COOKIE: trusted},
                    )
                    assert login.status_code == 200
                    assert "Check your inbox" in login.text

    arun(scenario())
