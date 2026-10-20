"""31i email-verification UX: the PRE-LOGIN GATE (owner decision 20 Oct).

Invariant: an unverified email NEVER completes login. After the password
check and BEFORE any OTP challenge, identity refuses with 403
email_unverified — no session is created and no sign-in OTP is issued.
The web app renders the dedicated gate screen ("Verify your email to
continue"), delivers a fresh verification link on arrival, and offers a
rate-limited resend plus a way back to sign-in.

Consequence: a signed-in user is ALWAYS verified, so every POST-AUTH
surface (dashboard, account page, profile blade in both modes, top
chrome) carries ZERO verification markup — no banner, no account-menu
tag, no resend action anywhere. The old signed-in resend route is gone.

Registration is unchanged: it still ends on the post-registration
check-your-inbox screen with its own resend.
"""
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from webapp.auth import SESSION_COOKIE

BANNED_POST_AUTH = (
    "unverified", "verification/resend", "Verify your email",
    "Resend verification", "activate your public page", "sv-tag-warn",
    "resent=ok", "resent=limited", "resent=failed",
)


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _register(idc: httpx.AsyncClient, email: str, org: str = "Gate Org") -> None:
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": org,
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text


def _verify_mails(email: str) -> list:
    from app.mailer import get_mailer

    return [
        m
        for m in get_mailer().outbox
        if m.to == email and m.subject.startswith("Verify your SeaVix")
    ]


async def _confirm_link(idc: httpx.AsyncClient, email: str) -> None:
    """Verify through the REAL link pipeline (send -> confirm)."""
    sent = await idc.post("/api/register/verify/send", json={"email": email})
    assert sent.status_code == 200, sent.text
    mail = _verify_mails(email)[-1]
    line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
    oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]
    ok = await idc.post(
        "/api/register/verify/confirm", json={"email": email, "oob_code": oob}
    )
    assert ok.status_code == 200, ok.text


def _assert_zero_verification_markup(html: str, where: str) -> None:
    for banned in BANNED_POST_AUTH:
        assert banned not in html, f"{where}: verification markup {banned!r}"


def test_unverified_correct_password_hits_gate_no_session_no_otp(arun, id_asgi):
    """Correct password + unverified email -> the gate screen; NO session
    cookie, NO OTP challenge, and a fresh verification link is delivered."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"gate-{u}@example.com"
            await _register(idc, email)

            async with _web() as w:
                before = len(_verify_mails(email))
                r = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert r.status_code == 200, r.text
                # the dedicated gate screen — not the OTP challenge
                assert "Verify your email to continue" in r.text
                assert f"<strong>{email}</strong>" in r.text
                assert "Click the link in that email, then sign in here" in r.text
                assert 'action="/login/verify-email/resend"' in r.text
                assert "Resend link" in r.text
                assert 'href="/login"' in r.text  # Back to sign in
                assert 'name="code"' not in r.text  # no OTP field
                # no session is established anywhere
                assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
                dash = await w.get("/dashboard", follow_redirects=False)
                assert dash.status_code == 303
                assert dash.headers["location"] == "/login"
                # NO sign-in OTP was issued for the unverified address
                peek = await idc.get(
                    "/api/dev/last-otp", params={"email": email}
                )
                assert peek.status_code == 404
                # ...and the gate auto-delivered exactly one verification link
                assert len(_verify_mails(email)) == before + 1

                # wrong password still gets the plain 401 (gate is post-check)
                bad = await w.post(
                    "/login", data={"email": email, "password": "nope-nope"}
                )
                assert bad.status_code == 401
                assert "invalid credentials" in bad.text

    arun(scenario())


def test_gate_resend_is_rate_limited(arun, id_asgi):
    """The gate resend goes through the identity verification-link pipeline
    (shared per-email 5/hour send window). The arrival send consumed one
    slot; four resends succeed, the fifth is refused with the inline error
    and no mail goes out."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"gateresend-{u}@example.com"
            await _register(idc, email)

            async with _web() as w:
                gate = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert "Verify your email to continue" in gate.text
                assert len(_verify_mails(email)) == 1  # the arrival send

                for i in range(4):  # sends 2-5 in the window
                    r = await w.post(
                        "/login/verify-email/resend", data={"email": email}
                    )
                    assert r.status_code == 200, r.text
                    assert "A new link is on its way." in r.text
                    assert "Verify your email to continue" in r.text
                    assert len(_verify_mails(email)) == 2 + i

                refused = await w.post(
                    "/login/verify-email/resend", data={"email": email}
                )
                assert refused.status_code == 200
                assert "too many requests" in refused.text
                assert len(_verify_mails(email)) == 5  # nothing more sent

    arun(scenario())


def test_verified_user_logs_in_normally_and_post_auth_is_verification_free(arun, id_asgi):
    """Verified email -> the normal OTP flow -> /dashboard. Every post-auth
    surface then carries ZERO verification markup, and the old signed-in
    resend route no longer exists."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"verified-{u}@example.com"
            await _register(idc, email)
            await _confirm_link(idc, email)  # the REAL link pipeline

            async with _web() as w:
                start = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert start.status_code == 200
                assert "Check your inbox" in start.text  # OTP challenge
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                ok = await w.post(
                    "/login/otp", data={"email": email, "code": code},
                    follow_redirects=False,
                )
                assert ok.status_code == 303
                assert ok.headers["location"] == "/dashboard"
                m = re.search(rf"{SESSION_COOKIE}=([^;]+)", ok.headers["set-cookie"])
                w.cookies.set(SESSION_COOKIE, m.group(1))

                dash = await w.get("/dashboard")
                assert dash.status_code == 200
                _assert_zero_verification_markup(dash.text, "dashboard")
                home = await w.get("/org/home")
                assert home.status_code == 200
                _assert_zero_verification_markup(home.text, "org home")
                blade = await w.get("/org/profile")
                assert blade.status_code == 200
                _assert_zero_verification_markup(blade.text, "profile blade")
                editor = await w.get("/org/profile?view=edit")
                _assert_zero_verification_markup(editor.text, "profile editor")
                # the account menu itself: no tag, just account + sign out
                topnav = re.search(
                    r'<nav class="sv-topnav".*?</nav>', dash.text, re.S
                ).group(0)
                assert 'href="/org/home"' in topnav
                assert 'action="/logout"' in topnav
                assert "unverified" not in topnav

                # the old signed-in resend route is gone entirely
                gone = await w.post("/org/verification/resend",
                                    follow_redirects=False)
                assert gone.status_code == 404

    arun(scenario())


def test_registration_flow_unchanged_by_the_gate(arun, id_asgi):
    """Registration still auto-sends the link and lands on the
    post-registration check-your-inbox screen (a pre-auth surface, not the
    gate); confirming it then allows a normal sign-in."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"reg-{u}@example.com"

            async with _web() as w:
                created = await w.post(
                    "/register",
                    data={
                        "org_name": "Gate Reg Org",
                        "org_type": "survey_company",
                        "email": email,
                        "password": "secret-123",
                        "confirm_password": "secret-123",
                    },
                )
                assert created.status_code == 200
                assert "verification link" in created.text.lower()
                assert 'action="/register/verify/resend"' in created.text
                assert len(_verify_mails(email)) == 1

                await _confirm_link(idc, email)
                start = await w.post(
                    "/login", data={"email": email, "password": "secret-123"}
                )
                assert "Check your inbox" in start.text

    arun(scenario())
