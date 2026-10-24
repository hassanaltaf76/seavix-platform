"""32a: unclaimed client orgs — POST /api/internal/orgs (+ GET by id).

Covers: creation with the full 31t contact block, NO membership / NO
app_user / NO Firebase account (the org is claimed later — the TODO hook
in register.py is deliberately NOT built, and one test proves it), the
loopback-only service guard (403 for non-internal callers), validation
parity with the profile editor, the org.created + client.created events,
and client.updated when a client org's profile is edited post-claim
(membership attached directly, exactly as the future claim hook will).
"""
import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client(**transport_kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **transport_kwargs),
        base_url="http://t",
    )


PAYLOAD = {
    "type": "client",
    "name": "Gulf Tanker Owners",
    "email": "ops@example.com",
    "contact_email": "desk@example.com",
    "phone_cc": "+971",
    "phone": "50 123 4567",
    "whatsapp_cc": "+971",
    "whatsapp": "50 765 4321",
    "address_line1": "Port Rashid Terminal",
    "address_line2": "Office 12",
    "city": "Dubai",
    "state": "DU",
    "postal_code": "12345",
    "country_code": "AE",
}


async def _create(c, **overrides) -> httpx.Response:
    body = {**PAYLOAD, "email": f"ops-{uuid.uuid4().hex[:8]}@example.com",
            **overrides}
    return await c.post("/api/internal/orgs", json=body)


def test_create_unclaimed_client_org(arun):
    async def scenario():
        async with _client() as c:
            events = []
            app.state.event_bus.subscribe("org.created", events.append)
            app.state.event_bus.subscribe("client.created", events.append)

            r = await _create(c, email=f"ops-{uuid.uuid4().hex[:8]}@example.com")
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["type"] == "client"
            org_id = body["org_id"]
            created_email = body["contact_email"]

            # events: lifecycle + domain, both on the EventBus port
            types = [e["type"] for e in events]
            assert "org.created" in types
            assert "client.created" in types
            created = next(e for e in events if e["type"] == "client.created")
            assert created["aggregate"] == "organization"
            assert created["aggregate_id"] == org_id
            assert created["payload"]["name"] == "Gulf Tanker Owners"

            # unclaimed: no membership, no app_user, no firebase account
            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                m = (await s.execute(
                    sql("SELECT 1 FROM membership WHERE org_id = :o"),
                    {"o": org_id})).first()
                assert m is None
                # 32b hardening: assert against THIS creation's email, not
                # a global pattern — the shared dev DB accumulates rows
                # from every suite that ever registered an 'ops-*' user.
                au = (await s.execute(
                    sql("SELECT 1 FROM app_user WHERE email = :e"),
                    {"e": created_email},
                )).first()
                assert au is None
                row = (await s.execute(
                    sql("SELECT type, contact_email, phone_cc, phone, city, "
                        "country_code FROM organization WHERE id = :o"),
                    {"o": org_id})).mappings().one()
                assert row["type"] == "client"
                assert row["contact_email"] == "desk@example.com"
                assert row["phone_cc"] == "+971"
                assert row["phone"] == "50 123 4567"
                assert row["city"] == "Dubai"
                assert row["country_code"] == "AE"

            # GET by id returns the directory fields
            g = await c.get(f"/api/internal/orgs/id/{org_id}")
            assert g.status_code == 200
            d = g.json()
            assert d["name"] == "Gulf Tanker Owners"
            assert d["contact_email"] == "desk@example.com"
            assert d["address_line1"] == "Port Rashid Terminal"
            assert d["whatsapp_cc"] == "+971"
            assert d["whatsapp"] == "50 765 4321"
            assert d["postal_code"] == "12345"

    arun(scenario())


def test_internal_org_endpoints_reject_non_loopback(arun):
    async def scenario():
        async with _client(client=("203.0.113.9", 4444)) as c:
            r = await _create(c)
            assert r.status_code == 403, r.text
            g = await c.get(f"/api/internal/orgs/id/{uuid.uuid4()}")
            assert g.status_code == 403, g.text

    arun(scenario())


def test_create_client_org_validation(arun):
    async def scenario():
        async with _client() as c:
            assert (await _create(c, country_code="UAE")).status_code == 422
            assert (await _create(c, phone="+971 50")).status_code == 422
            assert (await _create(c, phone="12")).status_code == 422
            assert (await _create(c, phone_cc="971")).status_code == 422
            assert (await _create(c, email="not-an-email")).status_code == 422
            # contact_email falls back to the claiming email
            r = await _create(c, contact_email=None)
            assert r.status_code == 201, r.text
            g = await c.get(f"/api/internal/orgs/id/{r.json()['org_id']}")
            assert g.json()["contact_email"].startswith("ops-")

    arun(scenario())


def test_registration_does_not_claim_yet(arun):
    """The 32a TODO hook is deliberately unbuilt: registering with the same
    email as an unclaimed client org still creates a FRESH org and leaves
    the unclaimed one without a membership."""
    async def scenario():
        email = f"claim-{uuid.uuid4().hex[:8]}@example.com"
        async with _client() as c:
            r = await c.post("/api/internal/orgs", json={
                "type": "client", "name": "Future Client Co", "email": email,
                "city": "Abu Dhabi", "country_code": "AE",
            })
            assert r.status_code == 201
            org_id = r.json()["org_id"]

            reg = await c.post("/api/register", json={
                "org_name": "Future Client Co",
                "org_type": "management",
                "email": email,
                "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg.status_code == 201, reg.text
            assert reg.json()["org_id"] != org_id

            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                m = (await s.execute(
                    sql("SELECT 1 FROM membership WHERE org_id = :o"),
                    {"o": org_id})).first()
                assert m is None  # still unclaimed

    arun(scenario())


def test_client_updated_event_on_profile_edit(arun):
    """Post-claim profile edit on a client org publishes client.updated.
    The membership is attached directly in the DB — exactly what the future
    claim hook will do."""
    async def scenario():
        u = uuid.uuid4().hex[:8]
        email = f"owner-{u}@example.com"
        async with _client() as c:
            r = await c.post("/api/internal/orgs", json={
                "type": "client", "name": f"Client {u}", "email": email,
                "country_code": "AE",
            })
            org_id = r.json()["org_id"]

            # claim-shaped membership: firebase user + app_user +
            # membership + the seeded owner role
            import uuid as _uuid

            from sqlalchemy import text as sql

            from app.db import SessionLocal
            from app.rbac import role_id_by_name, seed_default_roles

            async with httpx.AsyncClient(base_url="http://localhost:9099") as fc:
                fb = await fc.post(
                    "/identitytoolkit.googleapis.com/v1/accounts:signUp",
                    params={"key": "demo-any-key"},
                    json={"email": email, "password": "secret-123",
                          "returnSecureToken": True},
                )
            uid = str(_uuid.uuid4())
            async with SessionLocal() as s:
                await seed_default_roles(s, org_id)
                await s.execute(
                    sql("INSERT INTO app_user (id, email, firebase_uid, "
                        "email_verified) VALUES (:id, :email, :fb, true)"),
                    {"id": uid, "email": email, "fb": fb.json()["localId"]},
                )
                await s.execute(
                    sql("INSERT INTO membership (user_id, org_id, role) "
                        "VALUES (:uid, :org, 'owner')"),
                    {"uid": uid, "org": org_id},
                )
                rid = await role_id_by_name(s, org_id, "owner")
                await s.execute(
                    sql("INSERT INTO membership_role (user_id, org_id, role_id) "
                        "VALUES (:uid, :org, :rid)"),
                    {"uid": uid, "org": org_id, "rid": rid},
                )
                await s.commit()

            events = []
            app.state.event_bus.subscribe("client.updated", events.append)

            start = await c.post("/api/auth/login/start",
                                 json={"email": email, "password": "secret-123"})
            assert start.status_code == 200, start.text
            code = (await c.get("/api/dev/last-otp",
                                params={"email": email})).json()["code"]
            done = await c.post("/api/auth/login/verify",
                                json={"email": email, "code": code})
            assert done.status_code == 200, done.text
            sid = done.json()["session_id"]

            put = await c.put(
                "/api/orgs/me/profile",
                headers={"X-Seavix-Session": sid},
                json={"city": "Sharjah"},
            )
            assert put.status_code == 200, put.text
            assert len(events) == 1
            assert events[0]["type"] == "client.updated"
            assert events[0]["aggregate_id"] == org_id
            assert "city" in events[0]["payload"]["fields"]

    arun(scenario())
