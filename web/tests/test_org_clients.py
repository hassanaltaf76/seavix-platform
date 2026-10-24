"""32a: client directory pages — permission-gated (404-over-403), the rail
cell is permission-driven, and the full round trip runs through the REAL
work service in-process (identity + work + web on one loop; the work
service's own identity calls ride the ASGI transport seam).

Matrix (seeded roles):
- owner/admin/ops  client.view+client.create -> pages 200, rail cell, add
                     client -> detail -> list with relationship badge
- surveyor/viewer  no client.*               -> 404 pages, hidden rail,
                     POST /org/clients/new 404
- client (32a)     rfq.create+rfq.view ONLY  -> same 404s as surveyor
"""
import uuid

import httpx

from webapp.auth import SESSION_COOKIE

# (role name, seeded roles, can_view_clients, can_create)
MATRIX = [
    ("owner", ["owner"], True, True),
    ("admin", ["admin"], True, True),
    ("ops", ["ops"], True, True),
    ("surveyor", ["surveyor"], False, False),
    ("viewer", ["viewer"], False, False),
    ("client", ["client"], False, False),
]

FORM = {
    "name": "Harbor Shipmanagement",
    "relationship_type": "management",
    "email": "",
    "contact_email": "ops@harbor.example",
    "phone_cc": "+971",
    "phone": "50 123 4567",
    "whatsapp_cc": "+971",
    "whatsapp": "50 765 4321",
    "address_line1": "Pier 7",
    "address_line2": "",
    "city": "Dubai",
    "state": "",
    "postal_code": "",
    "country_code": "AE",
}


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _verify_email(email: str) -> None:
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _role_session(idc: httpx.AsyncClient, email: str, roles: list[str]) -> str:
    """Register a fresh org, swap the caller's own roles, and return the
    identity session id (the web session cookie)."""
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": "Clients Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    await _verify_email(email)
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


def test_clients_pages_rbac_matrix(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            for name, roles, can_view, can_create in MATRIX:
                email = f"clients-{name}-{u}@example.com"
                sid = await _role_session(idc, email, roles)
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sid)
                    home = await w.get("/org/home")
                    assert home.status_code == 200, (name, home.status_code)
                    index = await w.get("/org/clients")

                    if not can_view:
                        # 404-over-403: the page is a 404, never a 403
                        assert index.status_code == 404, (name, index.status_code)
                        assert "Page not found" in index.text
                        # rail cell hidden (permission-driven, not role name)
                        assert 'href="/org/clients"' not in home.text, name
                        # the POST surface fails closed the same way
                        post = await w.post(
                            "/org/clients/new",
                            data={**FORM, "email": f"x-{u}@example.com"},
                        )
                        assert post.status_code == 404, (name, post.status_code)
                        continue

                    assert index.status_code == 200, (name, index.text[:300])
                    assert "Client directory" in index.text
                    if can_create:
                        assert 'href="/org/clients/new"' in index.text, name
                        assert 'href="/org/clients"' in home.text, name
                        # create page carries the full 31t component set:
                        # country combobox + phone cc + whatsapp cc
                        new = await w.get("/org/clients/new")
                        assert new.status_code == 200, (name, new.text[:300])
                        assert new.text.count("sv-search-select-box") == 3, name
                        assert 'name="country_code"' in new.text
                        assert 'name="phone_cc"' in new.text
                        assert 'name="whatsapp_cc"' in new.text
                        assert "sv-save-bar" in new.text

                        # Save: PRG to the detail page
                        form = {**FORM, "email": f"client-{name}-{u}@example.com"}
                        saved = await w.post(
                            "/org/clients/new", data=form,
                            follow_redirects=False,
                        )
                        assert saved.status_code == 303, (name, saved.text[:300])
                        location = saved.headers["location"]
                        assert location.startswith("/org/clients/")
                        assert location.endswith("?created=1")

                        detail = await w.get(location)
                        assert detail.status_code == 200, (name, detail.text[:300])
                        assert "Harbor Shipmanagement" in detail.text
                        assert "ops@harbor.example" in detail.text
                        assert "+971 50 123 4567" in detail.text
                        assert "+971 50 765 4321" in detail.text
                        assert "Pier 7" in detail.text
                        assert "Dubai" in detail.text
                        assert "UNITED ARAB EMIRATES" in detail.text
                        # the RFQ section is an honest 32b placeholder
                        assert "task 32b" in detail.text

                        # list renders the row with the relationship badge
                        listing = await w.get("/org/clients")
                        assert "Harbor Shipmanagement" in listing.text
                        assert "Management" in listing.text
                        assert "ops@harbor.example" in listing.text

                        # unknown id -> the friendly 404
                        missing = await w.get(f"/org/clients/{uuid.uuid4()}")
                        assert missing.status_code == 404
                        assert "Page not found" in missing.text

                        # validation errors re-render inline (422 from work)
                        bad = await w.post(
                            "/org/clients/new",
                            data={**form, "name": f"Bad {name} {u}",
                                  "phone": "+971 50"},
                            follow_redirects=False,
                        )
                        assert bad.status_code == 422, (name, bad.text[:300])
                        assert "sv-field-error" in bad.text

    arun(scenario())


def test_clients_rail_cell_is_permission_driven(arun, id_asgi):
    """A CUSTOM role holding only client.view sees the rail cell and the
    list, but NOT the create page — the check is the permission."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"directory-{u}@example.com"
            sid = await _role_session(idc, email, ["owner"])
            auth = {"X-Seavix-Session": sid}
            made = await idc.post(
                "/api/orgs/me/roles",
                headers=auth,
                json={"name": "directory-reader", "permissions": ["client.view"]},
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
                assert 'href="/org/clients"' in home.text
                # profile blade stays hidden (no org.profile.view) but the
                # COMPANY section renders for the Clients entry alone
                assert 'href="/org/profile"' not in home.text
                index = await w.get("/org/clients")
                assert index.status_code == 200
                assert 'href="/org/clients/new"' not in index.text
                create = await w.get("/org/clients/new")
                assert create.status_code == 404
                post = await w.post(
                    "/org/clients/new",
                    data={**FORM, "email": f"y-{u}@example.com"},
                )
                assert post.status_code == 404

    arun(scenario())
