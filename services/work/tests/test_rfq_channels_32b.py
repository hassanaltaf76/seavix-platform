"""32b: two-channel RFQ intake at the work-service boundary.

- manual channel: POST /api/rfqs/manual — linked directory edge OR inline
  quick-create (identity org + edge land first); the client org is the
  requester, the tenant the target; channel/reference_no/vessel_name/
  port_name round-trip.
- rfq.created carries channel + client provenance.
- public funnel attribution: an authenticated submitter's org becomes the
  requester; an in-directory submitter's edge lands on client_id.
- reference_no + vessel_name round-trip on the spine create.

Identity is stubbed at the work.spine boundary (caller_org /
caller_member / resolve_public_org / create_identity_org), the same seam
pattern as test_work.py.
"""
import os
import sys
import uuid

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault(
    "WORK_DATABASE_URL", "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_work"
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import work.spine as spine  # noqa: E402
from work.main import app  # noqa: E402

# Per-test org ids live on the fixture handle (client.orgs) — the shared
# dev database enforces UNIQUE(tenant, client) on client_directory, so
# module-level constants would collide across tests.


async def fake_get_identity_org(org_id: str) -> dict:
    return {"id": org_id, "name": f"Org {org_id[:8]}"}


@pytest.fixture()
def client(arun, monkeypatch):
    state = {"org_id": None, "member": None}
    created_orgs = []
    # fresh org ids per test — client_directory has UNIQUE(tenant, client)
    # and these tests share the dev database with the rest of the suite.
    orgs = {"tenant": str(uuid.uuid4()), "client": str(uuid.uuid4()),
            "other": str(uuid.uuid4())}
    state["org_id"] = orgs["tenant"]
    # 32c: quote endpoints gate on permissions — the default member carries
    # them (plus the fixed uid/email the quote document stamps).
    state["member"] = {
        "org_id": orgs["tenant"], "slug": "stub", "type": "stub",
        "uid": str(uuid.uuid4()), "email": "ops@example.com",
        "permissions": {"quote.create", "quote.view", "quote.send",
                        "quote.accept", "rfq.create", "rfq.view",
                        "client.view", "client.create", "client.edit",
                        "job.view", "job.assign"},
    }

    async def fake_caller_org(request):
        return {"org_id": state["org_id"], "slug": "stub", "type": "stub"}

    async def fake_caller_member(request):
        if state["member"] is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        return state["member"]

    async def fake_resolve(slug: str):
        return {"org_id": orgs["tenant"], "slug": slug,
                "type": "survey_company",
                "profile_status": "live"}

    async def fake_create_identity_org(payload: dict) -> dict:
        org_id = str(uuid.uuid4())
        created_orgs.append(payload)
        return {"org_id": org_id, "slug": "stub", "type": "client",
                "contact_email": payload["email"]}

    monkeypatch.setattr(spine, "caller_org", fake_caller_org)
    monkeypatch.setattr(spine, "caller_member", fake_caller_member)
    monkeypatch.setattr(spine, "resolve_public_org", fake_resolve)
    monkeypatch.setattr(spine, "get_identity_org",
                        fake_get_identity_org)
    monkeypatch.setattr(spine, "create_identity_org", fake_create_identity_org)
    received = []
    app.state.event_bus.subscribe("rfq.created", received.append)
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://work")

    class Handle:
        def __init__(self):
            self.c = c
            self.state = state
            self.orgs = orgs
            self.events = received
            self.created_orgs = created_orgs
            self.run = arun

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            await self.c.aclose()

    return Handle()


MANUAL = {
    "channel": "manual_whatsapp",
    "vessel_name": "MT Sea Falcon",
    "vessel_imo": "9404572",
    "reference_no": "PMS-QT-38-2026",
    "port_name": "Karachi anchorage",
    "survey_types": ["condition"],
    "location_type": "anchorage",
    "preferred_date": "2026-11-01",
    "scope_notes": "On-hire condition + bunker",
}


async def _make_edge(tenant: str, client_org: str) -> str:
    from sqlalchemy import text

    from work.db import SessionLocal

    edge_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO client_directory (id, tenant_org_id, "
                "client_org_id, relationship_type, created_at) VALUES "
                "(:id, :tenant, :client, 'management', now())"
            ),
            {"id": edge_id, "tenant": tenant, "client": client_org},
        )
        await session.commit()
    return edge_id


def test_manual_rfq_linked_client(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT = client.orgs["tenant"], client.orgs["client"]
            edge_id = await _make_edge(TENANT, CLIENT)
            r = await c.post("/api/rfqs/manual",
                             json={**MANUAL, "client_id": edge_id})
            assert r.status_code == 201, r.text
            assert r.json()["status"] == "awaiting_quote"
            rfq_id = r.json()["id"]

            body = (await c.get(f"/api/rfqs/{rfq_id}")).json()
            assert body["channel"] == "manual_whatsapp"
            assert body["client_id"] == edge_id
            assert body["requesting_org_id"] == CLIENT  # client asks
            assert body["billing_org_id"] == CLIENT
            assert body["target_org_id"] == TENANT      # tenant quotes
            assert body["vessel_name"] == "MT Sea Falcon"
            assert body["reference_no"] == "PMS-QT-38-2026"
            assert body["port_name"] == "Karachi anchorage"
            assert body["status"] == "awaiting_quote"
            assert body["survey_types"] == ["condition"]

            event = client.events[-1]
            assert event["type"] == "rfq.created"
            assert event["payload"]["channel"] == "manual_whatsapp"
            assert event["payload"]["client_id"] == edge_id
            assert event["payload"]["requesting_org_id"] == CLIENT
            assert event["payload"]["target_org_id"] == TENANT

    client.run(scenario())


def test_manual_rfq_inline_quick_create(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT = client.orgs["tenant"], client.orgs["client"]
            r = await c.post("/api/rfqs/manual", json={
                **MANUAL, "channel": "manual_email",
                "new_client": {"name": "Vanguard Tech", "email": "asif@example.com",
                               "relationship_type": "management"},
            })
            assert r.status_code == 201, r.text
            body = (await c.get(f"/api/rfqs/{r.json()['id']}")).json()
            assert body["channel"] == "manual_email"
            # the quick-created org + edge exist and are wired in
            assert body["client_id"] is not None
            assert body["requesting_org_id"] is not None
            assert client.created_orgs[-1]["name"] == "Vanguard Tech"
            # the edge row points at the requesting org (same save)
            from sqlalchemy import text

            from work.db import SessionLocal

            async with SessionLocal() as session:
                edge = (
                    await session.execute(
                        text(
                            "SELECT client_org_id, relationship_type "
                            "FROM client_directory WHERE id = :id"
                        ),
                        {"id": body["client_id"]},
                    )
                ).mappings().one()
            assert str(edge["client_org_id"]) == body["requesting_org_id"]
            assert edge["relationship_type"] == "management"
            event = client.events[-1]
            assert event["payload"]["channel"] == "manual_email"
            assert event["payload"]["client_id"] == body["client_id"]

    client.run(scenario())


def test_manual_rfq_requires_exactly_one_client_ref(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT, OTHER = (client.orgs["tenant"], client.orgs["client"],
                                     client.orgs["other"])
            neither = await c.post("/api/rfqs/manual", json=MANUAL)
            assert neither.status_code == 422
            edge_id = await _make_edge(TENANT, CLIENT)
            both = await c.post("/api/rfqs/manual", json={
                **MANUAL, "client_id": edge_id,
                "new_client": {"name": "X", "email": "x@example.com",
                               "relationship_type": "port_agent"},
            })
            assert both.status_code == 422
            # an edge outside the caller's directory is a plain 404
            other_edge = await _make_edge(OTHER, CLIENT)
            missing = await c.post("/api/rfqs/manual",
                                   json={**MANUAL, "client_id": other_edge})
            assert missing.status_code == 404

    client.run(scenario())


def test_public_funnel_attributes_registered_submitter(client):
    async def scenario():
        async with client as c:
            TENANT, OTHER = client.orgs["tenant"], client.orgs["other"]
            # registered submitter, not yet in the tenant's directory:
            # attributed to their org, no client edge
            client.state["member"] = {"org_id": OTHER, "slug": "other",
                                      "type": "client", "permissions": []}
            r = await c.post("/api/public/rfqs", json={
                "org_slug": "live-surveyor", "survey_types": ["condition"],
                "location_type": "terminal", "vessel_name": "Barge 12",
                "reference_no": "REF-77",
            }, headers={"X-Seavix-Session": "sid"})
            assert r.status_code == 201, r.text
            body = (await c.get(f"/api/rfqs/{r.json()['id']}")).json()
            assert body["requesting_org_id"] == OTHER
            assert body["channel"] == "public_page"
            assert body["client_id"] is None
            assert body["vessel_name"] == "Barge 12"
            assert body["reference_no"] == "REF-77"
            event = client.events[-1]
            assert event["payload"]["channel"] == "public_page"
            assert event["payload"]["requesting_org_id"] == OTHER
            assert event["payload"]["client_id"] is None

            # in-directory submitter: the directory edge lands on client_id
            edge_id = await _make_edge(TENANT, OTHER)
            r2 = await c.post("/api/public/rfqs", json={
                "org_slug": "live-surveyor", "survey_types": ["condition"],
                "location_type": "terminal",
            }, headers={"X-Seavix-Session": "sid"})
            body2 = (await c.get(f"/api/rfqs/{r2.json()['id']}")).json()
            assert body2["client_id"] == edge_id
            assert client.events[-1]["payload"]["client_id"] == edge_id

    client.run(scenario())


def test_reference_vessel_round_trip_on_spine_create(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT = client.orgs["tenant"], client.orgs["client"]
            client.state["org_id"] = CLIENT
            r = await c.post("/api/rfqs", json={
                "target_org_id": TENANT, "survey_types": ["condition"],
                "location_type": "terminal", "vessel_name": "MV Test",
                "reference_no": "REF-1", "port_name": "Jebel Ali",
            })
            assert r.status_code == 201, r.text
            client.state["org_id"] = TENANT
            body = (await c.get(f"/api/rfqs/{r.json()['id']}")).json()
            assert body["vessel_name"] == "MV Test"
            assert body["reference_no"] == "REF-1"
            assert body["port_name"] == "Jebel Ali"
            assert body["channel"] == "manual_email"  # legacy spine default

    client.run(scenario())
