"""Browser auth-flow tests: login (password → OTP) → session cookie,
redirect by account kind, logout, password reset, and the registration
verification step. Web app + identity service in-process, one loop.
"""
import re
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE


def _extract_cookie(response: httpx.Response) -> str:
    set_cookie = response.headers.get("set-cookie", "")
    match = re.search(rf"{SESSION_COOKIE}=([^;]+)", set_cookie)
    assert match, f"no {SESSION_COOKIE} cookie in response"
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    return match.group(1)


async def _register_identity(c: httpx.AsyncClient, slug: str, email: str) -> None:
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Web Auth Org",
            "org_type": "marine",
            "slug": slug,
            "email": email,
            "password": "secret-123",
            "account_kind": "survey_company",
        },
    )
    assert resp.status_code == 201, resp.text


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


def test_login_flow_sets_session_cookie_and_redirects_by_kind(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            slug, email = f"wl-{u}", f"wl-{u}@example.com"
            await _register_identity(idc, slug, email)

            async with _web() as w:
                # login page renders
                form = await w.get("/login")
                assert form.status_code == 200
                assert "Sign in" in form.text

                # wrong password -> 401 page, no cookie
                bad = await w.post(
                    "/login",
                    data={"email": email, "password": "wrong-password"},
                )
                assert bad.status_code == 401
                assert SESSION_COOKIE not in bad.headers.get("set-cookie", "")

                # factor 1 ok -> OTP challenge page
                start = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert start.status_code == 200
                assert "6-digit" in start.text

                # wrong code -> 401 on the challenge page
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                wrong = "000000" if code != "000000" else "000001"
                bad_otp = await w.post(
                    "/login/otp", data={"email": email, "code": wrong}
                )
                assert bad_otp.status_code == 401

                # right code -> 303 + HttpOnly SameSite=Lax cookie
                ok = await w.post(
                    "/login/otp", data={"email": email, "code": code}
                )
                assert ok.status_code == 303
                assert ok.headers["location"] == "/org/profile"
                sid = _extract_cookie(ok)

                # authenticated page renders with the cookie
                w.cookies.set(SESSION_COOKIE, sid)
                home = await w.get("/org/profile")
                assert home.status_code == 200
                assert email in home.text
                assert slug in home.text

                # logout clears the cookie and kills the session
                w.cookies.set(SESSION_COOKIE, sid)
                out = await w.post("/logout", follow_redirects=False)
                assert out.status_code == 303
                cleared = out.headers.get("set-cookie", "")
                assert f"{SESSION_COOKIE}=" in cleared and "Max-Age=0" in cleared
                gone = await w.get("/org/profile", follow_redirects=False)
                assert gone.status_code == 303
                assert gone.headers["location"] == "/login"

    arun(scenario())


def test_client_login_redirects_to_org_home(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            slug, email = f"wc-{u}", f"wc-{u}@example.com"
            resp = await idc.post(
                "/api/register",
                json={
                    "org_name": "Client Org",
                    "org_type": "shipping",
                    "slug": slug,
                    "email": email,
                    "password": "secret-123",
                    "account_kind": "client",
                },
            )
            assert resp.status_code == 201

            async with _web() as w:
                await w.post("/login", data={"email": email, "password": "secret-123"})
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                ok = await w.post("/login/otp", data={"email": email, "code": code})
                assert ok.status_code == 303
                assert ok.headers["location"] == "/org/home"
                sid = _extract_cookie(ok)
                w.cookies.set(SESSION_COOKIE, sid)
                home = await w.get("/org/home")
                assert home.status_code == 200
                assert "client" in home.text

    arun(scenario())


def test_org_pages_require_session(arun):
    async def scenario():
        async with _web() as w:
            for path in ("/org/profile", "/org/home"):
                resp = await w.get(path, follow_redirects=False)
                assert resp.status_code == 303
                assert resp.headers["location"] == "/login"

    arun(scenario())


def test_register_flow_verifies_email(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            slug, email = f"wr-{u}", f"wr-{u}@example.com"
            async with _web() as w:
                form_page = await w.get("/register")
                assert form_page.status_code == 200

                created = await w.post(
                    "/register",
                    data={
                        "org_name": "Verify Me Ltd",
                        "org_type": "marine",
                        "slug": slug,
                        "email": email,
                        "password": "secret-123",
                        "account_kind": "survey_company",
                    },
                )
                assert created.status_code == 200
                # registration auto-sent the verification link
                assert "verification link" in created.text.lower()

                from urllib.parse import parse_qs, urlparse

                from app.mailer import get_mailer

                mail = next(
                    m for m in reversed(get_mailer().outbox)
                    if m.to == email and m.subject.startswith("Verify your SeaVix")
                )
                line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
                oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]

                confirm_page = await w.get(
                    "/register/verify/confirm",
                    params={"email": email, "oob_code": oob},
                )
                assert confirm_page.status_code == 200
                assert "Confirm" in confirm_page.text

                bad = await w.post(
                    "/register/verify",
                    data={"email": email, "oob_code": "bogus-code"},
                )
                assert bad.status_code == 400

                ok = await w.post(
                    "/register/verify", data={"email": email, "oob_code": oob}
                )
                assert ok.status_code == 200
                assert "verified" in ok.text.lower()

                me = await idc.get("/api/auth/session", headers={"X-Seavix-Session": ""})
                assert me.status_code == 401

    arun(scenario())


def test_password_reset_pages(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            slug, email = f"wp-{u}", f"wp-{u}@example.com"
            await _register_identity(idc, slug, email)

            async with _web() as w:
                fp = await w.get("/forgot-password")
                assert fp.status_code == 200
                sent = await w.post("/forgot-password", data={"email": email})
                assert sent.status_code == 200
                assert "reset link" in sent.text.lower()

            # the dev "mailbox" is the mail pipeline outbox
            from urllib.parse import parse_qs, urlparse

            from app.mailer import get_mailer

            mail = next(
                m for m in reversed(get_mailer().outbox)
                if m.to == email and m.subject.startswith("Reset your SeaVix")
            )
            line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
            oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]

            async with _web() as w:
                page = await w.get("/reset-password", params={"oob_code": oob})
                assert page.status_code == 200
                done = await w.post(
                    "/reset-password",
                    data={"oob_code": oob, "password": "new-secret-1"},
                    follow_redirects=False,
                )
                assert done.status_code == 303
                assert done.headers["location"] == "/login"

            start = await idc.post(
                "/api/auth/login/start",
                json={"email": email, "password": "new-secret-1"},
            )
            assert start.status_code == 200

    arun(scenario())
