"""RBAC tests (task 22): default role seeds, role CRUD guards, multi-role
union math, and the role x route permission matrix.

Harness: identity app in-process, single shared loop. Users authenticate
via the OTP login (session header) after registration; permissions are
asserted against the resolved AuthContext behavior at the route layer.
"""
import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.permissions import ALL_PERMISSIONS  # noqa: E402


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register(c, slug, email, kind="survey_company"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "RBAC Org",
            "org_type": "marine",
            "slug": slug,
            "email": email,
            "password": "secret-123",
            "account_kind": kind,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(c, email):
    start = await c.post(
        "/api/auth/login/start",
        json={"email": email, "password": "secret-123"},
    )
    assert start.status_code == 200, start.text
    code = (
        await c.get("/api/dev/last-otp", params={"email": email})
    ).json()["code"]
    done = await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    assert done.status_code == 200, done.text
    return done.json()["session_id"]


def _auth(sid):
    return {"X-Seavix-Session": sid}

async def _self_user_id(c, sid) -> str:
    me = await c.get("/api/auth/session", headers=_auth(sid))
    assert me.status_code == 200, me.text
    return me.json()["user_id"]



async def _seed_user(c, slug, email, roles=None):
    """Register + login a FRESH org, optionally replacing the owner role
    with `roles` (list of role names in that fresh org)."""
    await _register(c, slug, email)
    sid = await _login(c, email)
    if roles:
        listed = (
            await c.get("/api/orgs/me/roles", headers=_auth(sid))
        ).json()["roles"]
        ids = [r["role_id"] for r in listed if r["name"] in roles]
        uid = await _self_user_id(c, sid)
        put = await c.put(
            f"/api/orgs/me/members/{uid}/roles",
            headers=_auth(sid),
            json={"role_ids": ids},
        )
        assert put.status_code == 200, put.text
    uid = await _self_user_id(c, sid)
    return sid, uid


async def _add_member_to_org(org_id: str, email: str) -> str:
    """Emulator Firebase user + app_user + membership rows in `org_id`.
    Returns the new app_user id."""
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


# --------------------------------------------------------------- seeds

def test_default_roles_seeded_on_registration(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            await _register(c, f"rb-{u}", f"rb-{u}@example.com")
            sid = await _login(c, f"rb-{u}@example.com")

            resp = await c.get("/api/orgs/me/roles", headers=_auth(sid))
            assert resp.status_code == 200
            roles = {r["name"]: r for r in resp.json()["roles"]}
            assert set(roles) == {"owner", "admin", "ops", "surveyor", "viewer"}
            for r in roles.values():
                assert r["is_system"] is True
            assert set(roles["owner"]["permissions"]) == set(ALL_PERMISSIONS)
            assert "role.manage" not in roles["admin"]["permissions"]
            assert roles["viewer"]["permissions"] == sorted(
                {"org.profile.view", "rfq.view", "job.view"}
            )

            # the owner membership resolved through the AuthContext carries
            # the owner role and every permission
            me = await c.get("/api/internal/auth-context", headers=_auth(sid))
            membership = me.json()["memberships"][0]
            assert membership["roles"] == ["owner"]
            assert set(membership["permissions"]) == set(ALL_PERMISSIONS)

    arun(scenario())


# ------------------------------------------------------------ role CRUD

def test_role_crud_and_guards(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"cr-{u}@example.com"
            await _register(c, f"cr-{u}", email)
            sid = await _login(c, email)
            auth = _auth(sid)

            # create custom role from the catalog
            made = await c.post(
                "/api/orgs/me/roles",
                headers=auth,
                json={"name": "quotes",
                      "permissions": ["quote.create", "quote.send"]},
            )
            assert made.status_code == 201
            role_id = made.json()["role_id"]
            assert made.json()["is_system"] is False

            # duplicate name
            dup = await c.post(
                "/api/orgs/me/roles", headers=auth,
                json={"name": "quotes", "permissions": ["quote.create"]},
            )
            assert dup.status_code == 409

            # permission outside the system catalog is rejected (D2)
            bogus = await c.post(
                "/api/orgs/me/roles", headers=auth,
                json={"name": "rogue", "permissions": ["org.nuke"]},
            )
            assert bogus.status_code == 400

            # edit custom role
            upd = await c.put(
                f"/api/orgs/me/roles/{role_id}", headers=auth,
                json={"permissions": ["quote.create", "quote.send", "quote.accept"]},
            )
            assert upd.status_code == 200
            assert "quote.accept" in upd.json()["permissions"]

            # system roles are read-only
            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            owner = next(r for r in roles if r["name"] == "owner")
            for mutate in (
                c.put(f"/api/orgs/me/roles/{owner['role_id']}", headers=auth,
                      json={"name": "super-owner"}),
                c.delete(f"/api/orgs/me/roles/{owner['role_id']}", headers=auth),
            ):
                r = await mutate
                assert r.status_code == 409, r.text

            # delete blocked while assigned
            uid = await _self_user_id(c, sid)
            await c.put(
                f"/api/orgs/me/members/{uid}/roles", headers=auth,
                json={"role_ids": [role_id, owner["role_id"]]},
            )
            assigned = await c.delete(
                f"/api/orgs/me/roles/{role_id}", headers=auth
            )
            assert assigned.status_code == 409
            # unassign -> delete succeeds
            await c.put(
                f"/api/orgs/me/members/{uid}/roles", headers=auth,
                json={"role_ids": [owner["role_id"]]},
            )
            deleted = await c.delete(
                f"/api/orgs/me/roles/{role_id}", headers=auth
            )
            assert deleted.status_code == 200

    arun(scenario())


def test_role_management_requires_role_manage(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"rm-{u}@example.com"
            # viewer only
            sid, _ = await _seed_user(c, f"rm-{u}", email, roles=["viewer"])

            denied = await c.post(
                "/api/orgs/me/roles", headers=_auth(sid),
                json={"name": "x", "permissions": ["quote.create"]},
            )
            assert denied.status_code == 403
            assert "role.manage" in denied.json()["detail"]

            # listing roles needs only org.profile.view — viewer has it
            listed = await c.get("/api/orgs/me/roles", headers=_auth(sid))
            assert listed.status_code == 200

    arun(scenario())


# -------------------------------------------------------- multi-role union

def test_multi_role_union_semantics(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            owner_email = f"mu-{u}@example.com"
            org = await _register(c, f"mu-{u}", owner_email)
            org_id = org["org_id"]
            owner_sid = await _login(c, owner_email)
            owner_auth = _auth(owner_sid)

            # custom role with exactly one permission
            made = await c.post(
                "/api/orgs/me/roles", headers=owner_auth,
                json={"name": "quote-only", "permissions": ["quote.create"]},
            )
            quote_role = made.json()["role_id"]
            roles = (
                await c.get("/api/orgs/me/roles", headers=owner_auth)
            ).json()["roles"]
            viewer = next(r for r in roles if r["name"] == "viewer")
            ops = next(r for r in roles if r["name"] == "ops")

            # second member IN THE SAME ORG (rows scaffolded; task 23 ships
            # the invite API)
            member_email = f"m2-{u}@example.com"
            member_uid = await _add_member_to_org(org_id, member_email)
            member_sid = await _login(c, member_email)
            put = await c.put(
                f"/api/orgs/me/members/{member_uid}/roles", headers=owner_auth,
                json={"role_ids": [viewer["role_id"], quote_role]},
            )
            assert put.status_code == 200

            view = await c.get(
                f"/api/orgs/me/members/{member_uid}/roles", headers=owner_auth
            )
            effective = set(view.json()["effective_permissions"])
            # union: viewer set + quote.create (which viewer lacks)
            assert "quote.create" in effective
            assert "org.profile.view" in effective
            assert "role.manage" not in effective
            assert {r["name"] for r in view.json()["roles"]} == {
                "viewer", "quote-only"
            }

            # deny-does-not-override-allow: ops (no role.manage) + a custom
            # role that also lacks role.manage -> still no role.manage
            made2 = await c.post(
                "/api/orgs/me/roles", headers=owner_auth,
                json={"name": "nothing-new", "permissions": ["rfq.view"]},
            )
            put2 = await c.put(
                f"/api/orgs/me/members/{member_uid}/roles", headers=owner_auth,
                json={"role_ids": [ops["role_id"], made2.json()["role_id"]]},
            )
            assert put2.status_code == 200
            view2 = await c.get(
                f"/api/orgs/me/members/{member_uid}/roles", headers=owner_auth
            )
            eff2 = set(view2.json()["effective_permissions"])
            assert "quote.create" in eff2  # via ops
            assert "role.manage" not in eff2  # neither role grants it

            # union is enforced at the route layer too
            listed = await c.get("/api/orgs/me/roles", headers=_auth(member_sid))
            assert listed.status_code == 200  # ops grants org.profile.view
            forbidden = await c.put(
                "/api/orgs/me/profile", headers=_auth(member_sid),
                json={"about": "nope"},
            )
            assert forbidden.status_code == 403

    arun(scenario())


# ------------------------------------------------ role x route matrix

ROUTE_MATRIX = [
    # (method, path, permission, payload/factory)
    ("PUT", "/api/orgs/me/profile", "org.profile.edit", {"about": "x"}),
    ("POST", "/api/orgs/me/profile-status", "org.profile.publish", {"status": "draft"}),
    ("POST", "/api/orgs/me/roles", "role.manage", {"name": "m", "permissions": ["rfq.view"]}),
]


def test_role_route_permission_matrix(arun):
    """Each protected route rejects a role lacking its permission (403) and
    accepts a role holding exactly that permission (not 403)."""
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"mx-{u}@example.com"
            org = await _register(c, f"mx-{u}", email)
            org_id = org["org_id"]
            owner_sid = await _login(c, email)
            owner_auth = _auth(owner_sid)

            # member of the SAME org holding one "zero" role (rfq.create
            # only — no identity route permission)
            member_email = f"mm-{u}@example.com"
            member_uid = await _add_member_to_org(org_id, member_email)
            member_sid = await _login(c, member_email)
            member_auth = _auth(member_sid)
            zero = await c.post(
                "/api/orgs/me/roles", headers=owner_auth,
                json={"name": "zero", "permissions": ["rfq.create"]},
            )
            await c.put(
                f"/api/orgs/me/members/{member_uid}/roles", headers=owner_auth,
                json={"role_ids": [zero.json()["role_id"]]},
            )

            for method, path, permission, payload in ROUTE_MATRIX:
                if permission == "role.manage":
                    # viewer-with-rfq.create must not manage roles
                    r = await c.request(method, path, headers=member_auth, json=payload)
                    assert r.status_code == 403, (path, r.text)
                    # exact-permission role passes
                    exact = await c.post(
                        "/api/orgs/me/roles", headers=owner_auth,
                        json={"name": f"exact-{permission.split('.')[-1]}-{uuid.uuid4().hex[:6]}",
                              "permissions": [permission]},
                    )
                    assert exact.status_code == 201
                    await c.put(
                        f"/api/orgs/me/members/{member_uid}/roles",
                        headers=owner_auth,
                        json={"role_ids": [exact.json()["role_id"]]},
                    )
                    r2 = await c.request(method, path, headers=member_auth,
                                         json=payload)
                    assert r2.status_code != 403, (path, r2.text)
                    # back to zero
                    await c.put(
                        f"/api/orgs/me/members/{member_uid}/roles",
                        headers=owner_auth,
                        json={"role_ids": [zero.json()["role_id"]]},
                    )
                    continue
                r = await c.request(method, path, headers=member_auth, json=payload)
                assert r.status_code == 403, (path, r.text)
                assert permission in r.json()["detail"]

                exact = await c.post(
                    "/api/orgs/me/roles", headers=owner_auth,
                    json={"name": f"exact-{permission.split('.')[-1]}-{uuid.uuid4().hex[:6]}",
                          "permissions": [permission]},
                )
                assert exact.status_code == 201
                await c.put(
                    f"/api/orgs/me/members/{member_uid}/roles",
                    headers=owner_auth,
                    json={"role_ids": [exact.json()["role_id"]]},
                )
                r2 = await c.request(method, path, headers=member_auth, json=payload)
                assert r2.status_code != 403, (path, r2.text)
                # reset to the zero role
                await c.put(
                    f"/api/orgs/me/members/{member_uid}/roles",
                    headers=owner_auth,
                    json={"role_ids": [zero.json()["role_id"]]},
                )

            # logo upload row: org.logo.upload (multipart)
            png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32
            r = await c.post(
                "/api/orgs/me/logo", headers=member_auth,
                files={"file": ("l.png", png, "image/png")},
            )
            assert r.status_code == 403
            exact_logo = await c.post(
                "/api/orgs/me/roles", headers=owner_auth,
                json={"name": f"exact-logo-{uuid.uuid4().hex[:6]}",
                      "permissions": ["org.logo.upload"]},
            )
            await c.put(
                f"/api/orgs/me/members/{member_uid}/roles",
                headers=owner_auth,
                json={"role_ids": [exact_logo.json()["role_id"]]},
            )
            r2 = await c.post(
                "/api/orgs/me/logo", headers=member_auth,
                files={"file": ("l.png", png, "image/png")},
            )
            assert r2.status_code == 200, r2.text

    arun(scenario())
