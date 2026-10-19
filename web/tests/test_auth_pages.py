"""Browser auth-flow tests: login (password → OTP) → session cookie,
redirect by org type, logout, password reset (with confirm), OTP resend,
and the registration step (dropdown org type, double password, no slug).
Web app + identity service in-process, one loop.
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


async def _register_identity(c: httpx.AsyncClient, email: str,
                             org_type: str = "survey_company") -> dict:
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Web Auth Org",
            "org_type": org_type,
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


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
            email = f"wl-{u}@example.com"
            reg = await _register_identity(idc, email)
            slug = reg["slug"]

            async with _web() as w:
                # login page renders
                form = await w.get("/login")
                assert form.status_code == 200
                assert "Sign in" in form.text
                assert 'class="sv-auth"' in form.text

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
                assert "Check your inbox" in start.text

                # wrong code -> 401 on the challenge page
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                wrong = "000000" if code != "000000" else "000001"
                bad_otp = await w.post(
                    "/login/otp", data={"email": email, "code": wrong}
                )
                assert bad_otp.status_code == 401

                # resend issues a fresh code that works (anti-enum constant 200)
                resent = await w.post("/login/otp/resend", data={"email": email})
                assert resent.status_code == 200
                code2 = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                assert code2 != code

                # right code -> 303 + HttpOnly SameSite=Lax cookie.
                # 31c landing rule: everyone lands on org home (the one
                # surface every role can count on — the profile blade is
                # permission-gated and would 404 for surveyor/viewer)
                ok = await w.post(
                    "/login/otp", data={"email": email, "code": code2}
                )
                assert ok.status_code == 303
                assert ok.headers["location"] == "/org/home"
                sid = _extract_cookie(ok)

                # the profile blade still renders for the owner (view+edit)
                w.cookies.set(SESSION_COOKIE, sid)
                home = await w.get("/org/profile")
                assert home.status_code == 200
                assert email in home.text
                assert slug in home.text
                # 31b: profile blade is the public-page editor
                assert "Your public page" in home.text
                assert f"seavix.ai/{slug}" in home.text  # root-level framing
                assert "View public page" not in home.text  # draft: not live yet
                # rail: company name present, zero favorites/services/home items
                assert "Favorites" not in home.text
                assert "All services" not in home.text
                assert "SeaVix home" not in home.text
                assert 'href="/org/profile"' in home.text
                # 31b-7: the rendered blade never links the public landing,
                # and × closes to the parent crumb — following it lands on
                # org home
                assert 'href="/"' not in home.text
                close = re.search(r'class="sv-blade-close" href="([^"]+)"', home.text)
                assert close and close.group(1) == "/org/home"
                landing = await w.get(close.group(1))
                assert landing.status_code == 200
                assert "Your account" in landing.text
                # org home is the top-level blade: crumb root "Home", and
                # its × also closes to org home (never "/")
                assert "<li aria-current=\"page\">Home</li>" in landing.text
                top_close = re.search(
                    r'class="sv-blade-close" href="([^"]+)"', landing.text
                )
                assert top_close and top_close.group(1) == "/org/home"

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


def test_management_login_redirects_to_org_home(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"wc-{u}@example.com"
            await _register_identity(idc, email, org_type="management")

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
                assert "management" in home.text

    arun(scenario())


def test_org_pages_require_session(arun):
    async def scenario():
        async with _web() as w:
            for path in ("/org/profile", "/org/home"):
                resp = await w.get(path, follow_redirects=False)
                assert resp.status_code == 303
                assert resp.headers["location"] == "/login"

    arun(scenario())


def test_signed_out_only_dual_guard_on_auth_pages_and_landing(arun, id_asgi):
    """31c: the landing page and every auth-family page are signed-out-only
    chrome. The guard fires on BOTH the GET (form render) and the POST
    (form submit — the flow must not fire): a signed-in user always ends
    up on org home."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"wg-{u}@example.com"
            await _register_identity(idc, email)
            # a second account: proves a signed-in POST /login starts no
            # OTP challenge for it
            other = f"wi-{u}@example.com"
            await _register_identity(idc, other)

            async with _web() as w:
                await w.post("/login", data={"email": email, "password": "secret-123"})
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                ok = await w.post("/login/otp", data={"email": email, "code": code})
                sid = _extract_cookie(ok)
                w.cookies.set(SESSION_COOKIE, sid)

                # GET dual: landing + every auth page redirects home
                for path in ("/", "/login", "/register", "/forgot-password",
                             "/reset-password", "/login/otp",
                             "/register/verify/confirm"):
                    r = await w.get(path, follow_redirects=False)
                    assert r.status_code == 303, path
                    assert r.headers["location"] == "/org/home", path

                # POST dual: the login flow does not fire for `other`
                r = await w.post(
                    "/login",
                    data={"email": other, "password": "secret-123"},
                    follow_redirects=False,
                )
                assert r.status_code == 303
                assert r.headers["location"] == "/org/home"
                peek = await idc.get("/api/dev/last-otp", params={"email": other})
                assert peek.status_code == 404  # no challenge was started

                # POST dual: registration does not create the account
                new_email = f"wj-{u}@example.com"
                r = await w.post(
                    "/register",
                    data={
                        "org_name": "Guarded Org",
                        "org_type": "survey_company",
                        "email": new_email,
                        "password": "secret-123",
                        "confirm_password": "secret-123",
                    },
                    follow_redirects=False,
                )
                assert r.status_code == 303
                assert r.headers["location"] == "/org/home"
                probe = await idc.post(
                    "/api/auth/login/start",
                    json={"email": new_email, "password": "secret-123"},
                )
                assert probe.status_code == 401  # account never created

                # anonymous users still get the public landing and pages
                anon = _web()
                async with anon as a:
                    assert (await a.get("/")).status_code == 200
                    assert (await a.get("/login")).status_code == 200
                    assert (await a.get("/register")).status_code == 200

    arun(scenario())


def test_register_flow_dropdown_double_password_no_slug(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"wr-{u}@example.com"

            async with _web() as w:
                form_page = await w.get("/register")
                assert form_page.status_code == 200
                assert 'name="org_type"' in form_page.text
                assert 'name="confirm_password"' in form_page.text
                assert 'name="slug"' not in form_page.text

                # mismatched passwords -> inline error, NO account created
                mismatch = await w.post(
                    "/register",
                    data={
                        "org_name": "Verify Me Ltd",
                        "org_type": "survey_company",
                        "email": email,
                        "password": "secret-123",
                        "confirm_password": "different-1",
                    },
                )
                assert mismatch.status_code == 400
                assert "do not match" in mismatch.text.lower()
                probe = await idc.post(
                    "/api/auth/login/start",
                    json={"email": email, "password": "secret-123"},
                )
                assert probe.status_code == 401  # no account exists

                # unknown org type rejected before any identity call
                bad_type = await w.post(
                    "/register",
                    data={
                        "org_name": "Verify Me Ltd",
                        "org_type": "shipping",
                        "email": email,
                        "password": "secret-123",
                        "confirm_password": "secret-123",
                    },
                )
                assert bad_type.status_code == 400

                created = await w.post(
                    "/register",
                    data={
                        "org_name": "Verify Me Ltd",
                        "org_type": "survey_company",
                        "email": email,
                        "password": "secret-123",
                        "confirm_password": "secret-123",
                    },
                )
                assert created.status_code == 200
                # registration auto-sent the verification link
                assert "verification link" in created.text.lower()
                # registration success screen hints what's next
                assert "public profile" in created.text.lower()
                assert "go live" in created.text.lower()

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


def test_profile_settings_slug_update(arun, id_asgi):
    """The public page lives only in profile settings: register generates a
    placeholder, the owner sets the real slug + services text post-registration."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"ws-{u}@example.com"
            reg = await _register_identity(idc, email)
            placeholder = reg["slug"]
            new_slug = f"harbor-{u}"

            async with _web() as w:
                # login via OTP to get the session cookie
                await w.post("/login", data={"email": email, "password": "secret-123"})
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                ok = await w.post("/login/otp", data={"email": email, "code": code})
                sid = _extract_cookie(ok)
                w.cookies.set(SESSION_COOKIE, sid)

                # the editor lives in edit mode (31f view/edit split)
                page = await w.get("/org/profile?view=edit")
                assert page.status_code == 200
                assert placeholder in page.text
                assert "set it now or come back later" in page.text
                assert 'name="about"' in page.text

                # save description + slug together (PRG back to view mode)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": new_slug, "about": "Independent surveyors.",
                          "ports": []},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                assert saved.headers["location"] == "/org/profile?saved=1"
                saved_page = await w.get(saved.headers["location"])
                assert "Saved." in saved_page.text
                assert new_slug in saved_page.text
                assert f"seavix.ai/{new_slug}" in saved_page.text

                # the org is now reachable under the new root URL, with about
                pub = await idc.get(f"/api/orgs/{new_slug}")
                assert pub.status_code == 200
                assert pub.json()["about"] == "Independent surveyors."
                assert (await idc.get(f"/api/orgs/{placeholder}")).status_code == 404

                # go live (verification gate: confirm the email first)
                await idc.post("/api/register/verify/send", json={"email": email})
                from urllib.parse import parse_qs, urlparse

                from app.mailer import get_mailer

                mail = next(
                    m for m in reversed(get_mailer().outbox)
                    if m.to == email and m.subject.startswith("Verify your SeaVix")
                )
                line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
                oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]
                confirmed = await idc.post(
                    "/api/register/verify/confirm",
                    json={"email": email, "oob_code": oob},
                )
                assert confirmed.status_code == 200
                live = await idc.post(
                    "/api/orgs/me/profile-status",
                    headers={"X-Seavix-Session": sid},
                    json={"status": "live"},
                )
                assert live.status_code == 200

                # root-level public page renders for the live org
                page_live = await w.get(f"/{new_slug}")
                assert page_live.status_code == 200
                assert "Independent surveyors." in page_live.text
                assert "Raise an RFQ" in page_live.text

                # view mode now IS the public page content (31f preview);
                # the "Live page" tab opens the real thing in this blade
                editor = await w.get("/org/profile")
                assert editor.status_code == 200
                assert "sv-preview" in editor.text
                assert "Independent surveyors." in editor.text
                assert f'href="/{new_slug}"' in editor.text
                assert "Live page" in editor.text

                # legacy /p/<slug> redirects to the root URL
                legacy = await w.get(f"/p/{new_slug}", follow_redirects=False)
                assert legacy.status_code == 301
                assert legacy.headers["location"] == f"/{new_slug}"

                # reserved words can never be a public address
                reserved = await w.post("/org/profile", data={"slug": "login"})
                assert reserved.status_code == 422
                assert "reserved" in reserved.text

                # conflict with another org's slug -> inline field error
                other = await _register_identity(idc, f"other-{u}@example.com")
                conflict = await w.post("/org/profile", data={"slug": other["slug"]})
                assert conflict.status_code == 409
                assert "already taken" in conflict.text

                # bad format -> clean form error, not a 500
                bad = await w.post("/org/profile", data={"slug": "Bad Slug!"})
                assert bad.status_code == 422

    arun(scenario())


def test_password_reset_pages(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"wp-{u}@example.com"
            await _register_identity(idc, email)

            async with _web() as w:
                fp = await w.get("/forgot-password")
                assert fp.status_code == 200
                assert 'type="password"' not in fp.text  # email field only
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
                assert 'name="confirm_password"' in page.text

                # mismatch -> inline error, password unchanged
                mismatch = await w.post(
                    "/reset-password",
                    data={"oob_code": oob, "password": "new-secret-1",
                          "confirm_password": "other-secret"},
                )
                assert mismatch.status_code == 400
                assert "do not match" in mismatch.text.lower()

                done = await w.post(
                    "/reset-password",
                    data={"oob_code": oob, "password": "new-secret-1",
                          "confirm_password": "new-secret-1"},
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
