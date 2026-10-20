"""31f: profile VIEW mode is the public page (shared partial, WYSIWYG by
construction) and ports of operation end-to-end — Alpine combobox backend
search by name / UN/LOCODE / country, multi-add save + load round-trip,
chip removal, public page rendering, and unknown-code rejection.

Identity + registry + web all run in-process on the shared loop (see
web/tests/conftest.py); the registry carries the real UN/LOCODE import on
the dev DB, and the tests pin the canonical names of the ports they use.
"""
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from webapp.auth import SESSION_COOKIE

# Canonical UN/LOCODE rows the tests assert against (forced idempotently;
# matches the official import names).
PORTS = [
    {"code": "AEFJR", "name": "Al Fujayrah", "country": "AE"},
    {"code": "AEJEA", "name": "Jebel Ali", "country": "AE"},
    {"code": "SGSIN", "name": "Singapore", "country": "SG"},
    {"code": "NLRTM", "name": "Rotterdam", "country": "NL"},
]


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _pin_ports() -> None:
    from sqlalchemy import text as sql

    from registry.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql(
                "INSERT INTO port (code, name, country) VALUES "
                "(:code, :name, :country) "
                "ON CONFLICT (code) DO UPDATE SET "
                "name = EXCLUDED.name, country = EXCLUDED.country"
            ),
            PORTS,
        )
        await s.commit()


async def _verify_email(email: str) -> None:
    """31i: login gates on a verified email — mark it in the identity DB."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _register_and_login(idc, w, email, slug):
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "Preview Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    await _verify_email(email)  # 31i: login requires verified
    start = await w.post("/login", data={"email": email, "password": "secret-123"})
    assert "Check your inbox" in start.text  # OTP challenge, not the gate
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    ok = await w.post(
        "/login/otp", data={"email": email, "code": code}, follow_redirects=False
    )
    assert ok.status_code == 303
    set_cookie = ok.headers.get("set-cookie", "")
    sid = re.search(rf"{SESSION_COOKIE}=([^;]+)", set_cookie).group(1)
    w.cookies.set(SESSION_COOKIE, sid)
    return sid


async def _verify_email_and_go_live(idc, email, sid):
    await idc.post("/api/register/verify/send", json={"email": email})
    from app.mailer import get_mailer

    mail = next(
        m for m in reversed(get_mailer().outbox)
        if m.to == email and m.subject.startswith("Verify your SeaVix")
    )
    line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
    oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]
    confirmed = await idc.post(
        "/api/register/verify/confirm", json={"email": email, "oob_code": oob}
    )
    assert confirmed.status_code == 200, confirmed.text
    live = await idc.post(
        "/api/orgs/me/profile-status",
        headers={"X-Seavix-Session": sid},
        json={"status": "live"},
    )
    assert live.status_code == 200, live.text


def _article(html: str) -> str:
    # 31l: the article now carries class="sv-ugc" — match any attributes
    match = re.search(r"<article[^>]*>.*?</article>", html, re.S)
    assert match, "no <article> rendered"
    return match.group(0)


def test_view_mode_is_wysiwyg_and_ports_round_trip(arun, id_asgi):
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"pv-{u}@example.com"
            slug = f"preview-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)

                # save slug + about + three ports in one Save
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "ports": ["AEFJR", "SGSIN", "NLRTM"],
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                assert saved.headers["location"] == "/org/profile?saved=1"

                # VIEW MODE renders the public content — even while DRAFT
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                assert "Saved." in view.text
                assert "sv-preview" in view.text
                assert "draft" in view.text  # not public yet
                assert "Ports we serve" in view.text
                for name in ("Al Fujayrah", "Singapore", "Rotterdam"):
                    assert name in view.text
                assert "Independent marine surveyors." in view.text

                # EDIT MODE seeds the picker with the persisted selection
                # (JSON in x-data) and carries the chip + hidden-input
                # templates Alpine clones per selection
                editor = await w.get("/org/profile?view=edit")
                for code in ("AEFJR", "SGSIN", "NLRTM"):
                    assert f'"code": "{code}"' in editor.text
                assert editor.text.count('class="sv-chip"') == 1  # x-for template
                assert editor.text.count('name="ports"') == 1  # hidden-input template
                for name in ("Al Fujayrah", "Singapore", "Rotterdam"):
                    assert f'"name": "{name}"' in editor.text
                assert "remove(port.code)" in editor.text  # chip removal wiring

                # combobox backend: name / UN/LOCODE / country search
                by_name = await w.get("/org/ports/search?q=rotterdam")
                assert "NLRTM" in [p["code"] for p in by_name.json()["ports"]]
                by_code = await w.get("/org/ports/search?q=aefjr")
                assert "AEFJR" in [p["code"] for p in by_code.json()["ports"]]
                by_country = await w.get("/org/ports/search?q=AE")
                codes = [p["code"] for p in by_country.json()["ports"]]
                assert by_country.json()["ports"][0]["country"] == "AE"
                assert "AEJEA" in codes

                # chip removal round-trips: re-Save WITHOUT Singapore
                saved2 = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Independent marine surveyors.",
                          "ports": ["AEFJR", "NLRTM"]},
                    follow_redirects=False,
                )
                assert saved2.status_code == 303
                view2 = await w.get(saved2.headers["location"])
                assert "Singapore" not in view2.text
                assert "Al Fujayrah" in view2.text and "Rotterdam" in view2.text

                # public page (after verify + go live) renders the SAME
                # content partial — the articles must be identical
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/{slug}")
                assert pub.status_code == 200
                assert "Ports we serve" in pub.text
                assert "Al Fujayrah" in pub.text and "Rotterdam" in pub.text
                assert "Singapore" not in pub.text
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

    arun(scenario())


def test_unknown_port_codes_rejected(arun, id_asgi):
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"px-{u}@example.com"
            slug = f"ports-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                ok = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "ports": ["AEFJR"]},
                    follow_redirects=False,
                )
                assert ok.status_code == 303

                # wholly unknown code -> 422 with the field error, and the
                # previously saved ports are untouched
                bad = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "ports": ["XXXXX"]},
                )
                assert bad.status_code == 422
                assert "unknown port" in bad.text
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["ports"] == ["AEFJR"]

                # mixed known + unknown rejects the WHOLE save atomically
                mixed = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "ports": ["AEFJR", "ZZZZZ"]},
                )
                assert mixed.status_code == 422
                assert "unknown port" in mixed.text
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["ports"] == ["AEFJR"]

    arun(scenario())


def test_ports_search_requires_session(arun):
    async def scenario():
        async with _web() as w:
            r = await w.get("/org/ports/search?q=AE", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/login"

    arun(scenario())


def test_description_renders_as_escaped_blocks_31h(arun, id_asgi):
    """31h: the services description renders as BLOCKS on the public page
    AND the blade preview — blank lines delimit <p> blocks, single
    newlines become <br>, and user text is HTML-escaped first (a <script>
    typed in the textarea renders inert, never executes). Both surfaces
    share the content partial, so the articles stay byte-equal."""
    ABOUT = (
        "Independent marine surveyors.\n"
        "Serving the Gulf and beyond.\n"
        "\n"
        "Condition & bunker surveys.\n"
        "\n"
        "<script>alert('x')</script>"
    )
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"blocks-{u}@example.com"
            slug = f"blocks-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": ABOUT, "ports": []},
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                # draft preview: three <p> blocks inside the prose div
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                assert view.text.count('<div class="sv-prose">') == 1
                prose = re.search(
                    r'<div class="sv-prose">(.*?)</div>', view.text, re.S
                ).group(1)
                assert prose.count("<p>") == 3
                assert "Independent marine surveyors.<br>\nServing the Gulf" \
                    in view.text
                # escaped — the script can never execute
                assert "<script>alert" not in view.text
                assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" \
                    in view.text

                # go live, then the PUBLIC page renders the same blocks
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/{slug}")
                assert pub.status_code == 200
                assert pub.text.count('<div class="sv-prose">') == 1
                pub_prose = re.search(
                    r'<div class="sv-prose">(.*?)</div>', pub.text, re.S
                ).group(1)
                assert pub_prose.count("<p>") == 3
                assert "<script>alert" not in pub.text
                assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" \
                    in pub.text

                # preview == public, byte for byte (31f contract holds)
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

    arun(scenario())
