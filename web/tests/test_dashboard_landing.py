"""31g: /dashboard is the post-auth landing blade, and the owner-complaint
sweep is locked on the running code.

Landing rule (home_for):
- signed-in "/"                    -> 303 /dashboard
- post-login/OTP                   -> 303 /dashboard (every org type)
- crumb root "Home"                -> /dashboard (child blades link it)
- top-level × close                -> /dashboard

The dashboard renders for ANY active member (the deny-by-default wall plus
the loader's membership.status='active' filter), carries the master-only
verification banner (moved from org home), and keeps the rail company cell
permission-driven: org.profile.view holders see logo-or-initials + company
name linking to the profile blade; surveyor/viewer do not.
"""
import re
import uuid

import httpx

from webapp.auth import SESSION_COOKIE


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _role_session(idc: httpx.AsyncClient, email: str,
                        roles: list[str]) -> str:
    """Register a fresh org, swap the caller's own roles, and return the
    identity session id (same scaffolding as test_org_rbac)."""
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "Dashboard Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    start = await idc.post(
        "/api/auth/login/start",
        json={"email": email, "password": "secret-123"},
    )
    assert start.status_code == 200, start.text
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    done = await idc.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    assert done.status_code == 200, done.text
    sid = done.json()["session_id"]
    if roles:
        auth = {"X-Seavix-Session": sid}
        listed = (await idc.get("/api/orgs/me/roles", headers=auth)).json()["roles"]
        ids = [r["role_id"] for r in listed if r["name"] in roles]
        me = (await idc.get("/api/auth/session", headers=auth)).json()["user_id"]
        put = await idc.put(
            f"/api/orgs/me/members/{me}/roles",
            headers=auth,
            json={"role_ids": ids},
        )
        assert put.status_code == 200, put.text
    return sid


PLACEHOLDER = ("Your operational overview will live here — jobs, RFQs "
               "and activity arrive in upcoming releases.")


def test_signed_in_landing_and_post_login_land_on_dashboard(arun, id_asgi):
    """31g landing rule: the marketing landing is signed-out-only; a
    signed-in user bounces off "/" to /dashboard, and the OTP step lands
    there too. The dashboard is the top-level blade (crumb root "Home",
    × closes to itself) and nothing in the chrome links the public
    landing."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"dg-{u}@example.com"
            reg = await idc.post(
                "/api/register",
                json={
                    "org_name": "Dashboard Org",
                    "org_type": "survey_company",
                    "email": email,
                    "password": "secret-123",
                    "confirm_password": "secret-123",
                },
            )
            assert reg.status_code == 201, reg.text

            async with _web() as w:
                # signed-out "/" is still the marketing landing
                async with _web() as a:
                    landing = await a.get("/")
                    assert landing.status_code == 200
                    assert 'class="sv-public"' in landing.text

                # password -> OTP -> dashboard
                await w.post("/login", data={"email": email, "password": "secret-123"})
                code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
                ok = await w.post("/login/otp", data={"email": email, "code": code},
                                  follow_redirects=False)
                assert ok.status_code == 303
                assert ok.headers["location"] == "/dashboard"
                m = re.search(rf"{SESSION_COOKIE}=([^;]+)", ok.headers["set-cookie"])
                w.cookies.set(SESSION_COOKIE, m.group(1))

                # signed-in "/" redirects to the dashboard
                root = await w.get("/", follow_redirects=False)
                assert root.status_code == 303
                assert root.headers["location"] == "/dashboard"

                dash = await w.get("/dashboard")
                assert dash.status_code == 200
                assert "<h1>Dashboard</h1>" in dash.text
                assert PLACEHOLDER in dash.text
                # top-level blade: crumb root IS the page; × closes to itself
                assert '<li aria-current="page">Home</li>' in dash.text
                close = re.search(
                    r'class="sv-blade-close" href="([^"]+)"', dash.text
                )
                assert close and close.group(1) == "/dashboard"
                # chrome: rail logo roots at the dashboard, never the landing
                assert 'href="/"' not in dash.text
                assert 'href="/dashboard"' in dash.text

                # org home stays one click away as the account surface
                home = await w.get("/org/home")
                assert home.status_code == 200
                assert f"Signed in as {email}" in home.text
                assert "Your account" in home.text
                # its crumbs and × close to the dashboard
                assert '<li><a href="/dashboard">Home</a></li>' in home.text
                home_close = re.search(
                    r'class="sv-blade-close" href="([^"]+)"', home.text
                )
                assert home_close and home_close.group(1) == "/dashboard"

    arun(scenario())


# (role name, seeded roles, holds org.profile.view)
DASH_MATRIX = [
    ("owner", ["owner"], True),
    ("admin", ["admin"], True),
    ("ops", ["ops"], True),
    ("surveyor", ["surveyor"], False),
    ("viewer", ["viewer"], False),
]


def test_dashboard_renders_for_every_member_rail_cell_permission_driven(arun, id_asgi):
    """Any active member gets the dashboard; the rail company cell shows
    initials + company name for org.profile.view holders (owner/admin/
    ops) and is hidden for surveyor/viewer — visibility is the
    permission, never the role name."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            for name, roles, can_view in DASH_MATRIX:
                email = f"dash-{name}-{u}@example.com"
                sid = await _role_session(idc, email, roles)
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sid)
                    dash = await w.get("/dashboard")
                    assert dash.status_code == 200, (name, dash.status_code)
                    assert "<h1>Dashboard</h1>" in dash.text, name
                    assert PLACEHOLDER in dash.text, name
                    if can_view:
                        assert "sv-rail-tenant" in dash.text, name
                        assert "sv-rail-initials" in dash.text, name
                        # initials derived from the company name (2 words ->
                        # first letters, uppercased)
                        assert ">DO</" in dash.text.replace("\n", ""), name
                        assert "Dashboard Org" in dash.text, name
                        assert 'href="/org/profile"' in dash.text, name
                    else:
                        assert "sv-rail-tenant" not in dash.text, name
                        assert "sv-rail-initials" not in dash.text, name
                        assert 'href="/org/profile"' not in dash.text, name
                    # the account surface and the sign-out stay reachable
                    assert "Your account" in dash.text
                    assert 'action="/logout"' in dash.text

    arun(scenario())


def test_save_is_disabled_until_dirty(arun, id_asgi):
    """31g (2c): the edit-mode Save button renders disabled — the Alpine
    component seeds dirty=false and binds :disabled="!dirty"; typing in
    about/slug or adding/removing a port flips it (exercised for real in
    the headless-Chrome sweep; here the wiring is pinned)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sv-{u}@example.com"
            sid = await _role_session(idc, email, ["owner"])
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, sid)
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                # one form-wide Alpine component; the Save binding present
                assert 'class="sv-form"\n        x-data=' in editor.text
                assert "dirty: false" in editor.text
                assert ':disabled="!dirty"' in editor.text
                assert editor.text.count('@input="dirty = true"') == 2  # about + slug
                # the disabled styling ships in the stylesheet
                import pathlib

                css = (pathlib.Path(__file__).resolve().parents[1]
                       / "static" / "style.css").read_text()
                assert "button:disabled" in css

    arun(scenario())
