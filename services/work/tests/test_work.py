"""Work service spine tests — RFQ → quote revisions → job.

Identity HTTP calls are stubbed at the work.spine boundary (caller_org /
resolve_public_org); the shared event loop comes from conftest.py.
"""
import os
import uuid

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault(
    "WORK_DATABASE_URL", "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_work"
)

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import work.spine as spine  # noqa: E402
from work.main import app  # noqa: E402

CLIENT_ORG = str(uuid.uuid4())
SURVEY_ORG = str(uuid.uuid4())


@pytest.fixture()
def client(arun, monkeypatch):
    """ASGI client + identity stubs. caller() switches the authenticated org."""
    state = {"org_id": CLIENT_ORG, "enforce_auth": False}

    async def fake_caller_org(request):
        if state["enforce_auth"] and "authorization" not in request.headers:
            raise HTTPException(status_code=401, detail="missing bearer token")
        return {"org_id": state["org_id"], "slug": "stub", "type": "stub"}

    async def fake_resolve(slug: str):
        if slug == "live-surveyor":
            return {"org_id": SURVEY_ORG, "slug": slug, "type": "survey_company",
                    "profile_status": "live"}
        if slug == "draft-surveyor":
            raise HTTPException(status_code=400,
                                detail="organization is not a live survey company")
        raise HTTPException(status_code=404, detail="organization not found")

    monkeypatch.setattr(spine, "caller_org", fake_caller_org)
    monkeypatch.setattr(spine, "resolve_public_org", fake_resolve)
    received = []
    app.state.event_bus.subscribe("rfq.created", received.append)
    app.state.event_bus.subscribe("quote.sent", received.append)
    app.state.event_bus.subscribe("quote.accepted", received.append)
    app.state.event_bus.subscribe("job.created", received.append)

    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://work")

    class Handle:
        def __init__(self):
            self.c = c
            self.state = state
            self.events = received
            self.run = arun

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            await self.c.aclose()

    return Handle()


ITEMS_V1 = [{"description": "Attendance", "amount": 1000, "unit": "day"}]
ITEMS_V2 = [{"description": "Attendance", "amount": 950, "unit": "day"}]


def test_full_rfq_quote_job_flow(client):
    async def scenario():
        async with client as c:
            # 1. internal RFQ from the client org
            client.state["org_id"] = CLIENT_ORG
            r = await c.post("/api/rfqs", json={
                "target_org_id": SURVEY_ORG, "vessel_imo": "1024948",
                "survey_type": "condition", "location_type": "anchorage",
                "preferred_date": "2026-11-01", "scope_notes": "Annual survey",
            })
            assert r.status_code == 201
            rfq_id = r.json()["id"]
            assert client.events[-1]["type"] == "rfq.created"

            # billing defaults to requesting org
            g = await c.get(f"/api/rfqs/{rfq_id}")
            assert g.json()["billing_org_id"] == CLIENT_ORG
            assert g.json()["requesting_org_id"] == CLIENT_ORG

            # 2. survey org quotes rev1, sends it
            client.state["org_id"] = SURVEY_ORG
            q1 = await c.post(f"/api/rfqs/{rfq_id}/quotes",
                              json={"line_items": ITEMS_V1, "currency": "usd"})
            assert q1.status_code == 201 and q1.json()["revision"] == 1
            q1_id = q1.json()["id"]
            s1 = await c.post(f"/api/quotes/{q1_id}/send")
            assert s1.status_code == 200 and s1.json()["status"] == "sent"
            assert client.events[-1]["type"] == "quote.sent"
            assert (await c.get(f"/api/rfqs/{rfq_id}")).json()["status"] == "quoted"

            # sent quote cannot be re-sent
            assert (await c.post(f"/api/quotes/{q1_id}/send")).status_code == 409

            # 3. rev2 drafted — rev1 (sent) is superseded, line items untouched
            q2 = await c.post(f"/api/rfqs/{rfq_id}/quotes",
                              json={"line_items": ITEMS_V2, "currency": "usd"})
            assert q2.json()["revision"] == 2
            q2_id = q2.json()["id"]
            g2 = await c.get(f"/api/rfqs/{rfq_id}")
            quotes = {q["revision"]: q for q in g2.json()["quotes"]}
            assert quotes[1]["status"] == "superseded"
            assert quotes[1]["line_items"] == ITEMS_V1  # immutable
            assert quotes[2]["status"] == "draft"

            # wrong org cannot send rev2
            client.state["org_id"] = CLIENT_ORG
            assert (await c.post(f"/api/quotes/{q2_id}/send")).status_code == 403
            client.state["org_id"] = SURVEY_ORG
            await c.post(f"/api/quotes/{q2_id}/send")

            # 4. client accepts rev2 → job in the same transaction
            client.state["org_id"] = CLIENT_ORG
            acc = await c.post(f"/api/quotes/{q2_id}/accept")
            assert acc.status_code == 201
            job_id = acc.json()["job_id"]

            g3 = await c.get(f"/api/rfqs/{rfq_id}")
            body = g3.json()
            assert body["status"] == "accepted"
            quotes = {q["revision"]: q for q in body["quotes"]}
            assert quotes[2]["status"] == "accepted"
            assert quotes[1]["status"] == "superseded"
            assert quotes[1]["line_items"] == ITEMS_V1

            # job row: owner is the survey org, billing defaulted to client
            job = (await c.get(f"/api/jobs/{job_id}")).json()
            assert job["status"] == "scheduled"
            assert job["owner_org_id"] == SURVEY_ORG
            assert job["requesting_org_id"] == CLIENT_ORG
            assert job["billing_org_id"] == CLIENT_ORG
            assert job["rfq_id"] == rfq_id and job["quote_id"] == q2_id
            assert job["survey_type"] == "condition" and job["vessel_imo"] == "1024948"

            # events order: quote.accepted then job.created
            types = [e["type"] for e in client.events]
            assert types[-2:] == ["quote.accepted", "job.created"]

            # 5. job is queryable state: rfq listing by status works
            listed = (await c.get("/api/rfqs", params={"status": "accepted"})).json()
            assert any(x["id"] == rfq_id for x in listed["rfqs"])

    client.run(scenario())


def test_decline_path(client):
    async def scenario():
        async with client as c:
            client.state["org_id"] = CLIENT_ORG
            rfq_id = (await c.post("/api/rfqs", json={
                "target_org_id": SURVEY_ORG, "survey_type": "bunker",
                "location_type": "terminal"})).json()["id"]
            client.state["org_id"] = SURVEY_ORG
            q = (await c.post(f"/api/rfqs/{rfq_id}/quotes",
                              json={"line_items": ITEMS_V1, "currency": "eur"})).json()
            # cannot decline a draft
            client.state["org_id"] = CLIENT_ORG
            assert (await c.post(f"/api/quotes/{q['id']}/decline")).status_code == 409
            client.state["org_id"] = SURVEY_ORG
            await c.post(f"/api/quotes/{q['id']}/send")
            # survey org cannot decline its own quote
            assert (await c.post(f"/api/quotes/{q['id']}/decline")).status_code == 403
            # requesting org declines
            client.state["org_id"] = CLIENT_ORG
            d = await c.post(f"/api/quotes/{q['id']}/decline")
            assert d.status_code == 200 and d.json()["status"] == "declined"

    client.run(scenario())


def test_public_intake(client):
    async def scenario():
        async with client as c:
            ok = await c.post("/api/public/rfqs", json={
                "org_slug": "live-surveyor", "contact_email": " buyer@example.com ",
                "survey_type": "pre-purchase", "location_type": "STS",
                "vessel_imo": "9404572", "scope_notes": "Pre-purchase survey",
            })
            assert ok.status_code == 201
            rfq_id = ok.json()["id"]
            body = (await c.get(f"/api/rfqs/{rfq_id}")).json()
            assert body["source"] == "public"
            assert body["requesting_org_id"] is None     # anonymous (decision a)
            assert body["billing_org_id"] is None
            assert body["contact_email"] == "buyer@example.com"  # EmailStr trims
            assert body["target_org_id"] == SURVEY_ORG

            # non-live org rejected
            bad = await c.post("/api/public/rfqs", json={
                "org_slug": "draft-surveyor", "contact_email": "b@example.com",
                "survey_type": "condition", "location_type": "OPL"})
            assert bad.status_code == 400
            # unknown org rejected
            missing = await c.post("/api/public/rfqs", json={
                "org_slug": "nope", "contact_email": "b@example.com",
                "survey_type": "condition", "location_type": "OPL"})
            assert missing.status_code == 404

            # anonymous RFQ cannot be accepted yet (requester conversion later)
            client.state["org_id"] = SURVEY_ORG
            q = (await c.post(f"/api/rfqs/{rfq_id}/quotes",
                              json={"line_items": ITEMS_V1, "currency": "usd"})).json()
            await c.post(f"/api/quotes/{q['id']}/send")
            client.state["org_id"] = CLIENT_ORG
            acc = await c.post(f"/api/quotes/{q['id']}/accept")
            assert acc.status_code == 409

    client.run(scenario())


def test_auth_required(client):
    async def scenario():
        async with client as c:
            client.state["enforce_auth"] = True
            client.state["org_id"] = CLIENT_ORG
            r = await c.post("/api/rfqs", json={
                "target_org_id": SURVEY_ORG, "survey_type": "condition",
                "location_type": "terminal"},
                headers={"Authorization": "Bearer test"})
            assert r.status_code == 201
            noauth = await c.post("/api/rfqs", json={
                "target_org_id": SURVEY_ORG, "survey_type": "condition",
                "location_type": "terminal"})
            assert noauth.status_code == 401

    client.run(scenario())
