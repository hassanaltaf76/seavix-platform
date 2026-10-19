"""Tenant user management + cross-tenant isolation (task 23).

Invite lifecycle (D4), deactivate/reactivate with immediate access loss,
and the 404-over-403 isolation policy: org B's resources are invisible to
org A's credentials. Org-scoping audit of every management query.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _auth(sid):
    return {"X-Seavix-Session": sid}


async def _register(c, email, org_type="survey_company"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Tenancy Org",
            "org_type": org_type,
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(c, email, expect=200, password="secret-123"):
    start = await c.post(
        "/api/auth/login/start",
        json={"email": email, "password": password},
    )
    assert start.status_code == 200, start.text
    code = (
        await c.get("/api/dev/last-otp", params={"email": email})
    ).json()["code"]
    done = await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    assert done.status_code == expect, done.text
    if expect != 200:
        return None
    return done.json()["session_id"]


async def _self_user_id(c, sid):
    me = await c.get("/api/auth/session", headers=_auth(sid))
    return me.json()["user_id"]


async def _invite_token(c, email):
    resp = await c.get("/api/dev/last-invite", params={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ------------------------------------------------------------- invites

def test_invite_happy_path(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            owner_email = f"iv-{u}@example.com"
            await _register(c, owner_email)
            owner_sid = await _login(c, owner_email)
            auth = _auth(owner_sid)

            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            ops = next(r for r in roles if r["name"] == "ops")

            member_email = f"im-{u}@example.com"
            made = await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": member_email, "role_ids": [ops["role_id"]]},
            )
            assert made.status_code == 201
            assert made.json()["email"] == member_email

            # duplicate invite for an existing member is rejected
            dup = await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": owner_email, "role_ids": [ops["role_id"]]},
            )
            assert dup.status_code == 409

            # invitee accepts: sets password, membership + roles created,
            # verification code auto-sent
            token = await _invite_token(c, member_email)
            accepted = await c.post(
                "/api/public/invites/accept",
                json={"token": token, "password": "their-secret-1"},
            )
            assert accepted.status_code == 201
            assert accepted.json()["verification_required"] is True

            # replay of the consumed token fails
            replay = await c.post(
                "/api/public/invites/accept",
                json={"token": token, "password": "their-secret-1"},
            )
            assert replay.status_code == 404
            garbage = await c.post(
                "/api/public/invites/accept",
                json={"token": "x" * 40, "password": "their-secret-1"},
            )
            assert garbage.status_code == 404

            # invited member verifies email via the link sent through the
            # mail pipeline, then logs in with the standing OTP flow
            from urllib.parse import parse_qs, urlparse

            from app.mailer import get_mailer

            mail = next(
                m for m in reversed(get_mailer().outbox)
                if m.to == member_email and m.subject.startswith("Verify your SeaVix")
            )
            line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
            oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]
            ok = await c.post(
                "/api/register/verify/confirm",
                json={"email": member_email, "oob_code": oob},
            )
            assert ok.status_code == 200

            sid = await _login(c, member_email, password="their-secret-1")
            me = await c.get("/api/internal/auth-context", headers=_auth(sid))
            membership = me.json()["memberships"][0]
            assert membership["roles"] == ["ops"]
            assert "quote.create" in membership["permissions"]
            assert "role.manage" not in membership["permissions"]
            assert me.json()["email_verified"] is True

            # ops can list roles (org.profile.view) but not create them
            listed = await c.get("/api/orgs/me/roles", headers=_auth(sid))
            assert listed.status_code == 200
            denied = await c.post(
                "/api/orgs/me/roles", headers=_auth(sid),
                json={"name": "x", "permissions": ["quote.create"]},
            )
            assert denied.status_code == 403

            # owner sees the member + pending invite history
            members = (
                await c.get("/api/orgs/me/members", headers=auth)
            ).json()["members"]
            entry = next(m for m in members if m["email"] == member_email)
            assert entry["status"] == "active"
            assert entry["roles"] == ["ops"]
            invites = (
                await c.get("/api/orgs/me/invites", headers=auth)
            ).json()["invites"]
            assert invites[0]["email"] == member_email
            assert invites[0]["accepted_at"] is not None

    arun(scenario())


def test_invite_guards(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            owner_email = f"ig-{u}@example.com"
            org_a = await _register(c, owner_email)
            owner_sid = await _login(c, owner_email)
            auth = _auth(owner_sid)

            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            viewer = next(r for r in roles if r["name"] == "viewer")

            # role from ANOTHER org -> 404 (isolation)
            other_email = f"og-{u}@example.com"
            await _register(c, other_email)
            other_sid = await _login(c, other_email)
            other_roles = (
                await c.get("/api/orgs/me/roles", headers=_auth(other_sid))
            ).json()["roles"]
            foreign = await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": f"fk-{u}@example.com",
                      "role_ids": [other_roles[0]["role_id"]]},
            )
            assert foreign.status_code == 404

            # expired invite -> generic 404 on accept
            member_email = f"ex-{u}@example.com"
            await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": member_email,
                      "role_ids": [viewer["role_id"]]},
            )
            token = await _invite_token(c, member_email)
            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                await s.execute(
                    sql("UPDATE invite SET expires_at = :past WHERE email = :e"),
                    {"past": datetime.now(timezone.utc) - timedelta(minutes=1),
                     "e": member_email},
                )
                await s.commit()
            expired = await c.post(
                "/api/public/invites/accept",
                json={"token": token, "password": "their-secret-1"},
            )
            assert expired.status_code == 404

            # viewer cannot invite (demote self last: viewer cannot undo it)
            me = await _self_user_id(c, owner_sid)
            await c.put(
                f"/api/orgs/me/members/{me}/roles", headers=auth,
                json={"role_ids": [viewer["role_id"]]},
            )
            forbidden = await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": f"zz-{u}@example.com", "role_ids": [viewer["role_id"]]},
            )
            assert forbidden.status_code == 403

    arun(scenario())


# ------------------------------------------------- deactivate/reactivate

def test_deactivate_reactivate_member(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            owner_email = f"dc-{u}@example.com"
            await _register(c, owner_email)
            owner_sid = await _login(c, owner_email)
            auth = _auth(owner_sid)
            org_id = (
                await c.get("/api/auth/session", headers=auth)
            ).json()["memberships"][0]["org_id"]

            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            ops = next(r for r in roles if r["name"] == "ops")

            member_email = f"dm-{u}@example.com"
            await c.post(
                "/api/orgs/me/invites", headers=auth,
                json={"email": member_email, "role_ids": [ops["role_id"]]},
            )
            token = await _invite_token(c, member_email)
            await c.post(
                "/api/public/invites/accept",
                json={"token": token, "password": "their-secret-1"},
            )
            member_sid = await _login(c, member_email, password="their-secret-1")
            member_id = await _self_user_id(c, member_sid)

            # live session works pre-deactivation
            ok = await c.get("/api/orgs/me/roles", headers=_auth(member_sid))
            assert ok.status_code == 200

            off = await c.post(
                f"/api/orgs/me/members/{member_id}/deactivate", headers=auth
            )
            assert off.status_code == 200
            assert off.json()["sessions_revoked"] >= 1

            # session revoked at once
            dead = await c.get("/api/auth/session", headers=_auth(member_sid))
            assert dead.status_code == 401
            # new login blocked
            blocked = await _login(c, member_email, expect=403, password="their-secret-1")
            assert blocked is None
            # AuthContext for any still-alive session would carry no membership
            # (history preserved in the DB)
            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                row = (
                    await s.execute(
                        sql(
                            "SELECT status FROM membership WHERE user_id = :uid"
                        ),
                        {"uid": member_id},
                    )
                ).scalar_one()
                assert row == "deactivated"
                kept = (
                    await s.execute(
                        sql(
                            "SELECT count(*) FROM membership_role WHERE user_id = :uid"
                        ),
                        {"uid": member_id},
                    )
                ).scalar_one()
                assert kept == 1  # role history preserved

            on = await c.post(
                f"/api/orgs/me/members/{member_id}/reactivate", headers=auth
            )
            assert on.status_code == 200
            member_sid2 = await _login(c, member_email, password="their-secret-1")
            alive = await c.get("/api/orgs/me/roles", headers=_auth(member_sid2))
            assert alive.status_code == 200

    arun(scenario())


# ------------------------------------------------------ cross-tenant 404s

def test_cross_tenant_isolation(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            a_email, b_email = f"ta-{u}@example.com", f"tb-{u}@example.com"
            await _register(c, a_email)
            await _register(c, b_email)
            a_sid = await _login(c, a_email)
            b_sid = await _login(c, b_email)
            a_auth, b_auth = _auth(a_sid), _auth(b_sid)

            # B creates a member and a custom role
            b_roles = (
                await c.get("/api/orgs/me/roles", headers=b_auth)
            ).json()["roles"]
            b_ops = next(r for r in b_roles if r["name"] == "ops")
            custom = await c.post(
                "/api/orgs/me/roles", headers=b_auth,
                json={"name": "b-custom", "permissions": ["quote.create"]},
            )
            b_member_email = f"tm-{u}@example.com"
            await c.post(
                "/api/orgs/me/invites", headers=b_auth,
                json={"email": b_member_email, "role_ids": [b_ops["role_id"]]},
            )
            token = await _invite_token(c, b_member_email)
            await c.post(
                "/api/public/invites/accept",
                json={"token": token, "password": "their-secret-1"},
            )
            b_members = (
                await c.get("/api/orgs/me/members", headers=b_auth)
            ).json()["members"]
            b_member_id = next(
                m["user_id"] for m in b_members if m["email"] == b_member_email
            )

            # A pokes at B's resources: everything 404s (404-over-403)
            r1 = await c.put(
                f"/api/orgs/me/members/{b_member_id}/roles", headers=a_auth,
                json={"role_ids": [b_ops["role_id"]]},
            )
            assert r1.status_code == 404
            r2 = await c.get(
                f"/api/orgs/me/members/{b_member_id}/roles", headers=a_auth
            )
            assert r2.status_code == 403 or r2.status_code == 404  # member.manage first
            r3 = await c.delete(
                f"/api/orgs/me/roles/{custom.json()['role_id']}", headers=a_auth
            )
            assert r3.status_code == 404
            r4 = await c.put(
                f"/api/orgs/me/roles/{custom.json()['role_id']}", headers=a_auth,
                json={"name": "hijack"},
            )
            assert r4.status_code == 404
            r5 = await c.post(
                f"/api/orgs/me/members/{b_member_id}/deactivate", headers=a_auth
            )
            assert r5.status_code == 404

            # A's member list never contains B's members
            a_members = (
                await c.get("/api/orgs/me/members", headers=a_auth)
            ).json()["members"]
            assert all(m["email"] != b_member_email for m in a_members)
            assert {m["email"] for m in a_members} == {a_email}

            # B is untouched
            b_members2 = (
                await c.get("/api/orgs/me/members", headers=b_auth)
            ).json()["members"]
            assert any(m["email"] == b_member_email for m in b_members2)

    arun(scenario())
