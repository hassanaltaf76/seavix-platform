"""31e email-verification UX (AMENDED per owner feedback): the unverified
tag (or ANY account-level verification state) must NEVER render on the
company profile blade or its public preview — on a public-content surface
it misreads as "the company is unverified".

Where the state lives:
- the signed-in ACCOUNT MENU (base.html topnav) — a subtle, actionable
  tag that POSTs the resend;
- a banner on ORG HOME for unverified MASTER accounts only ("Verify your
  email to activate your public page." + Resend verification email).

The resend goes through the identity verification-link pipeline,
rate-limited like every other send (shared per-email 5/hour window).
Verified accounts see neither tag nor banner, and the blade stays clean.
"""
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from webapp.auth import SESSION_COOKIE


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


def _sid_from(response: httpx.Response) -> str:
    set_cookie = response.headers.get("set-cookie", "")
    match = re.search(rf"{SESSION_COOKIE}=([^;]+)", set_cookie)
    assert match, f"no {SESSION_COOKIE} cookie in response"
    return match.group(1)


def _verify_mails(email: str) -> list:
    from app.mailer import get_mailer

    return [
        m
        for m in get_mailer().outbox
        if m.to == email and m.subject.startswith("Verify your SeaVix")
    ]


def _last_oob(email: str) -> str:
    mail = _verify_mails(email)[-1]
    line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
    return parse_qs(urlparse(line.strip()).query)["oob_code"][0]


def _topnav(html: str) -> str:
    return re.search(r'<nav class="sv-topnav".*?</nav>', html, re.S).group(0)


def _blade_body(html: str) -> str:
    """The blade content proper — account MENU chrome excluded."""
    return re.search(r'<div class="sv-blade-body".*', html, re.S).group(0)


def _assert_no_verification_state(html: str, email: str) -> None:
    body = _blade_body(html)
    for banned in ("unverified", "verification/resend", "Verify your email",
                   "Signed in as", "Roles:", email):
        assert banned not in body, f"verification/account state on blade: {banned!r}"


async def _register_and_login_web(idc: httpx.AsyncClient, w: httpx.AsyncClient,
                                  email: str) -> None:
    """Identity-register (no verification mail sent by that API) then web
    login; leaves the web client carrying the session cookie."""
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "Verify UX Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    start = await w.post("/login", data={"email": email, "password": "secret-123"})
    assert start.status_code == 200, start.text
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    ok = await w.post(
        "/login/otp", data={"email": email, "code": code}, follow_redirects=False
    )
    assert ok.status_code == 303
    w.cookies.set(SESSION_COOKIE, _sid_from(ok))


async def _identity_session(idc: httpx.AsyncClient, email: str,
                            password: str = "secret-123") -> str:
    start = await idc.post(
        "/api/auth/login/start", json={"email": email, "password": password}
    )
    assert start.status_code == 200, start.text
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    done = await idc.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    assert done.status_code == 200, done.text
    return done.json()["session_id"]


async def _add_member(org_id: str, email: str) -> str:
    """Emulator Firebase user + app_user + membership rows (same scaffolding
    pattern as services/identity/tests/test_rbac.py)."""
    import uuid as _uuid

    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with httpx.AsyncClient(base_url="http://localhost:9099") as fc:
        resp = await fc.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signUp",
            params={"key": "demo-any-key"},
            json={"email": email, "password": "secret-123",
                  "returnSecureToken": True},
        )
    fb_uid = resp.json()["localId"]
    user_id = str(_uuid.uuid4())
    async with SessionLocal() as s:
        await s.execute(
            sql(
                "INSERT INTO app_user (id, email, firebase_uid) "
                "VALUES (:id, :email, :fb)"
            ),
            {"id": user_id, "email": email, "fb": fb_uid},
        )
        await s.execute(
            sql(
                "INSERT INTO membership (user_id, org_id, role) "
                "VALUES (:uid, :org, 'member')"
            ),
            {"uid": user_id, "org": org_id},
        )
        await s.commit()
    return user_id


def test_unverified_master_account_menu_tag_and_org_home_banner(arun, id_asgi):
    """Unverified master: banner on org home, actionable tag in the account
    menu — and ZERO verification markup on the profile blade (view and
    edit) or its public preview. Resend works; verifying clears both."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"vx-{u}@example.com"
            async with _web() as w:
                await _register_and_login_web(idc, w, email)

                home = await w.get("/org/home")
                assert home.status_code == 200
                # the master banner lives on ORG HOME
                assert "Verify your email to activate your public page." in home.text
                assert 'method="post" action="/org/verification/resend"' in home.text
                # the subtle actionable tag lives in the ACCOUNT MENU
                assert "unverified — resend" in _topnav(home.text)
                # org home is the account surface: the email line stays here
                assert f"Signed in as {email}" in home.text

                # the profile blade carries NO account/verification state
                blade = await w.get("/org/profile")
                _assert_no_verification_state(blade.text, email)
                editor = await w.get("/org/profile?view=edit")
                _assert_no_verification_state(editor.text, email)

                # resend from the account menu: PRG + a REAL mail
                before = len(_verify_mails(email))
                r = await w.post("/org/verification/resend", follow_redirects=False)
                assert r.status_code == 303
                assert r.headers["location"] == "/org/home?resent=ok"
                assert len(_verify_mails(email)) == before + 1
                followed = await w.get(r.headers["location"])
                assert "on its way" in followed.text

                # verify through the link that was just resent
                oob = _last_oob(email)
                confirmed = await idc.post(
                    "/api/register/verify/confirm",
                    json={"email": email, "oob_code": oob},
                )
                assert confirmed.status_code == 200

                # verified: tag AND banner are gone; the blade was always clean
                home2 = await w.get("/org/home")
                assert "activate your public page" not in home2.text
                assert "unverified" not in _topnav(home2.text)
                blade2 = await w.get("/org/profile")
                _assert_no_verification_state(blade2.text, email)

                # resending a verified account is a plain no-op redirect
                noop = await w.post("/org/verification/resend", follow_redirects=False)
                assert noop.status_code == 303
                assert noop.headers["location"] == "/org/home"

    arun(scenario())


def test_unverified_non_master_tag_in_menu_but_no_banner(arun, id_asgi):
    """The activation banner is for the master (owner) account only; other
    unverified members still get the account-menu tag (never blade state)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            owner_email = f"no-{u}@example.com"
            reg = await idc.post(
                "/api/register",
                json={
                    "org_name": "Non Master Org",
                    "org_type": "survey_company",
                    "email": owner_email,
                    "password": "secret-123",
                    "confirm_password": "secret-123",
                },
            )
            assert reg.status_code == 201, reg.text
            owner_sid = await _identity_session(idc, owner_email)
            auth = {"X-Seavix-Session": owner_sid}
            org_id = (await idc.get("/api/auth/session", headers=auth)).json()[
                "memberships"
            ][0]["org_id"]

            member_email = f"nm-{u}@example.com"
            member_uid = await _add_member(org_id, member_email)
            roles = (await idc.get("/api/orgs/me/roles", headers=auth)).json()["roles"]
            viewer = next(r for r in roles if r["name"] == "viewer")
            put = await idc.put(
                f"/api/orgs/me/members/{member_uid}/roles",
                headers=auth,
                json={"role_ids": [viewer["role_id"]]},
            )
            assert put.status_code == 200, put.text

            member_sid = await _identity_session(idc, member_email)
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, member_sid)
                home = await w.get("/org/home")
                assert home.status_code == 200
                # account-menu tag yes, master banner NO (viewer is not
                # the master account)
                assert "unverified — resend" in _topnav(home.text)
                assert "activate your public page" not in home.text
                # the blade stays clean for non-masters too
                blade = await w.get("/org/profile", follow_redirects=False)
                assert blade.status_code == 404  # viewer: no profile access
                _assert_no_verification_state(blade.text, member_email)

                # the member's resend works too
                before = len(_verify_mails(member_email))
                r = await w.post("/org/verification/resend", follow_redirects=False)
                assert r.status_code == 303
                assert r.headers["location"] == "/org/home?resent=ok"
                assert len(_verify_mails(member_email)) == before + 1

    arun(scenario())


def test_resend_is_rate_limited(arun, id_asgi):
    """31e: the resend uses the existing verification-link pipeline,
    rate-limited as the other sends — one shared per-email 5/hour window.
    The login OTP consumed one slot; the fifth resend is refused."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"vl-{u}@example.com"
            async with _web() as w:
                await _register_and_login_web(idc, w, email)

                for i in range(4):  # budget: login send + 4 resends = 5
                    r = await w.post("/org/verification/resend",
                                     follow_redirects=False)
                    assert r.status_code == 303
                    assert r.headers["location"] == "/org/home?resent=ok", i
                r = await w.post("/org/verification/resend", follow_redirects=False)
                assert r.status_code == 303
                assert r.headers["location"] == "/org/home?resent=limited"

                page = await w.get(r.headers["location"])
                assert "too many requests" in page.text
                # exactly four verification mails went out for this account
                assert len(_verify_mails(email)) == 4

    arun(scenario())
