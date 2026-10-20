"""31f: org ports of operation — persistence behind PUT /api/orgs/me/profile
and the public read model (GET /api/orgs/{slug} carries UN/LOCODE codes).

port_code is a cross-service reference to registry.port: identity stores
codes (format-validated), the registry owns existence/name data.
"""
import sys
import uuid

import httpx

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register(c, email):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Ports Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _verify_email(c, email):
    """31i: login gates on a verified email. The helper marks the address
    verified directly in the dev DB so the per-email SEND window stays
    pristine for send-budget assertions."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()


async def _login(c, email):
    await _verify_email(c, email)
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


def test_ports_of_operation_round_trip_replace_and_format(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"op-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            # public read model starts empty
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.status_code == 200
            assert pub.json()["ports"] == []

            # save a multi-port list (dedupe in the validator)
            r = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={"ports": ["SGSIN", "AEFJR", "SGSIN", "NLRTM"]},
            )
            assert r.status_code == 200, r.text
            assert r.json()["ports"] == ["SGSIN", "AEFJR", "NLRTM"]

            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["ports"] == ["AEFJR", "NLRTM", "SGSIN"]  # code order

            # replace: the Save button submits the complete chip list
            r = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"ports": ["BEANR"]},
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["ports"] == ["BEANR"]

            # clear with an empty list; absent field leaves ports alone
            r = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"ports": []}
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["ports"] == []
            r = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"about": "hi"}
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["ports"] == []

            # format guard: unknown-shape codes are rejected before any write
            for bad in (["abcde"], ["TOOLONG"], ["AB1"], ["AEFJ!"]):
                r = await c.put(
                    "/api/orgs/me/profile", headers=auth, json={"ports": bad}
                )
                assert r.status_code == 422, (bad, r.text)
            # ...and a rejected write changed nothing
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["ports"] == []

    arun(scenario())


def test_ports_require_profile_edit(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"pe-{u}@example.com"
            await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            # demote self to viewer (no org.profile.edit)
            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            viewer = next(r for r in roles if r["name"] == "viewer")
            me = (await c.get("/api/auth/session", headers=auth)).json()["user_id"]
            put = await c.put(
                f"/api/orgs/me/members/{me}/roles", headers=auth,
                json={"role_ids": [viewer["role_id"]]},
            )
            assert put.status_code == 200, put.text

            denied = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"ports": ["SGSIN"]}
            )
            assert denied.status_code == 403
            assert "org.profile.edit" in denied.json()["detail"]

    arun(scenario())
