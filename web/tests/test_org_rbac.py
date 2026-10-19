"""31e: the profile blade is permission-gated — test matrix over the
seeded roles, exercised through the WEB app (blade template + rail cell +
POST surfaces), with identity running in-process.

Matrix (seeded roles, org.profile.* permissions):
- owner    view+edit+logo -> full editor blade, editable forms
- admin    view+edit+logo -> full editor blade, editable forms
- ops      view only     -> read-only blade (NO form, NO Edit button),
                            POST /org/profile -> 404, rail cell visible
- surveyor no access     -> GET /org/profile 404 (404-over-403), rail
                            company cell hidden, org-home link hidden
- viewer   no access     -> same as surveyor

Visibility is asserted on the RENDERED pages and is permission-driven
(the rail cell and links check membership.permissions, never role names).
"""
import uuid

import httpx

from webapp.auth import SESSION_COOKIE

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _role_session(idc: httpx.AsyncClient, email: str, roles: list[str]) -> str:
    """Register a fresh org, swap the caller's own roles, and return the
    identity session id (used as the web session cookie)."""
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "RBAC Blade Org",
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
    auth = {"X-Seavix-Session": sid}
    if roles:
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


# (role name, seeded roles, can_view, can_edit)
MATRIX = [
    ("owner", ["owner"], True, True),
    ("admin", ["admin"], True, True),
    ("ops", ["ops"], True, False),
    ("surveyor", ["surveyor"], False, False),
    ("viewer", ["viewer"], False, False),
]


def test_profile_blade_rbac_matrix(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            for name, roles, can_view, can_edit in MATRIX:
                email = f"blade-{name}-{u}@example.com"
                sid = await _role_session(idc, email, roles)
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sid)
                    blade = await w.get("/org/profile")
                    home = await w.get("/org/home")
                    assert home.status_code == 200, (name, home.status_code)

                    if not can_view:
                        # 404-over-403: the blade is a 404, never a 403
                        assert blade.status_code == 404, (name, blade.status_code)
                        assert "Page not found" in blade.text
                        # rail company cell hidden ...
                        assert 'href="/org/profile"' not in home.text, name
                        assert "sv-rail-tenant" not in home.text, name
                        # ... and the org-home shortcut is gone as well
                        assert "Edit your public page" not in home.text, name
                        assert "View your public page" not in home.text, name
                        # the POST surfaces fail closed the same way
                        post = await w.post(
                            "/org/profile",
                            data={"slug": f"x-{u}", "about": "nope"},
                        )
                        assert post.status_code == 404, (name, post.status_code)
                        logo = await w.post(
                            "/org/profile/logo",
                            files={"file": ("l.png", _PNG, "image/png")},
                        )
                        assert logo.status_code == 404, (name, logo.status_code)
                        continue

                    assert blade.status_code == 200, (name, blade.text[:300])
                    # rail cell present and links to the blade
                    assert 'href="/org/profile"' in home.text, name
                    if can_edit:
                        # full editor: forms render
                        assert 'name="about"' in blade.text, name
                        assert 'name="slug"' in blade.text, name
                        assert 'action="/org/profile/logo"' in blade.text, name
                        assert "Edit your public page" in home.text, name
                        # and saving works
                        saved = await w.post(
                            "/org/profile",
                            data={"slug": f"{name}-saved-{u}", "about": "hi"},
                        )
                        assert saved.status_code == 200, (name, saved.text[:300])
                        assert "Saved." in saved.text, name
                    else:
                        # view-only: read-only blade, NO form, NO Edit button
                        assert 'name="about"' not in blade.text, name
                        assert 'name="slug"' not in blade.text, name
                        assert 'action="/org/profile/logo"' not in blade.text, name
                        assert "view-only access" in blade.text, name
                        # org-home link is labelled by capability
                        assert "View your public page" in home.text, name
                        assert "Edit your public page" not in home.text, name
                        # POSTing an edit is the same 404 as the blade
                        post = await w.post(
                            "/org/profile",
                            data={"slug": f"x-{u}", "about": "nope"},
                        )
                        assert post.status_code == 404, (name, post.status_code)
                        logo = await w.post(
                            "/org/profile/logo",
                            files={"file": ("l.png", _PNG, "image/png")},
                        )
                        assert logo.status_code == 404, (name, logo.status_code)

    arun(scenario())


def test_rail_cell_is_permission_driven_not_role_name_driven(arun, id_asgi):
    """A CUSTOM role holding only org.profile.view sees the rail cell and
    the read-only blade — the check is the permission, not the role name."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"custom-{u}@example.com"
            sid = await _role_session(idc, email, ["owner"])
            auth = {"X-Seavix-Session": sid}
            made = await idc.post(
                "/api/orgs/me/roles",
                headers=auth,
                json={"name": "page-reader", "permissions": ["org.profile.view"]},
            )
            assert made.status_code == 201, made.text
            me = (await idc.get("/api/auth/session", headers=auth)).json()["user_id"]
            put = await idc.put(
                f"/api/orgs/me/members/{me}/roles",
                headers=auth,
                json={"role_ids": [made.json()["role_id"]]},
            )
            assert put.status_code == 200, put.text

            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, sid)
                home = await w.get("/org/home")
                assert 'href="/org/profile"' in home.text
                blade = await w.get("/org/profile")
                assert blade.status_code == 200
                # view permission only: read-only, no edit form
                assert "view-only access" in blade.text
                assert 'name="about"' not in blade.text
                post = await w.post(
                    "/org/profile", data={"slug": f"y-{u}", "about": "nope"}
                )
                assert post.status_code == 404

    arun(scenario())
