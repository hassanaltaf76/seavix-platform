"""32d: RFQ survey_types — catalog-backed multi-select.

The join (rfq_survey_type) is authoritative; the legacy rfq.survey_type
column keeps the FIRST selected code so existing readers keep working.
Identity is stubbed at the work.spine boundary (caller_org /
resolve_public_org), the same seam pattern as test_work.py.
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

CLIENT_ORG = str(uuid.uuid4())
SURVEY_ORG = str(uuid.uuid4())


@pytest.fixture()
def client(arun, monkeypatch):
    async def fake_caller_org(request):
        return {"org_id": CLIENT_ORG, "slug": "stub", "type": "stub"}

    async def fake_resolve(slug: str):
        return {"org_id": SURVEY_ORG, "slug": slug, "type": "survey_company",
                "profile_status": "live"}

    monkeypatch.setattr(spine, "caller_org", fake_caller_org)
    monkeypatch.setattr(spine, "resolve_public_org", fake_resolve)
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://work")

    class Handle:
        def __init__(self):
            self.c = c
            self.run = arun

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            await self.c.aclose()

    return Handle()


def test_rfq_multi_survey_types_round_trip(client):
    async def scenario():
        async with client as c:
            r = await c.post("/api/rfqs", json={
                "target_org_id": SURVEY_ORG, "vessel_imo": "1024948",
                "survey_types": ["pre_purchase", "condition"],
                "location_type": "anchorage",
            })
            assert r.status_code == 201, r.text
            rfq_id = r.json()["id"]

            g = await c.get(f"/api/rfqs/{rfq_id}")
            assert g.status_code == 200
            body = g.json()
            # the join is authoritative (sorted by code on read)
            assert body["survey_types"] == ["condition", "pre_purchase"]
            # legacy column carries the FIRST submitted code
            assert body["survey_type"] == "pre_purchase"

    client.run(scenario())


def test_public_intake_multi_survey_types(client):
    async def scenario():
        async with client as c:
            r = await c.post("/api/public/rfqs", json={
                "org_slug": "live-surveyor", "contact_email": "c@example.com",
                "survey_types": ["bunker_quantity", "custom_thing"],
                "location_type": "STS",
            })
            assert r.status_code == 201, r.text
            g = await c.get(f"/api/rfqs/{r.json()['id']}")
            assert sorted(g.json()["survey_types"]) == [
                "bunker_quantity", "custom_thing"]

    client.run(scenario())


def test_survey_types_validation(client):
    async def scenario():
        async with client as c:
            base = {
                "target_org_id": SURVEY_ORG,
                "survey_types": ["condition"],
                "location_type": "anchorage",
            }
            # empty list rejected
            r = await c.post("/api/rfqs", json={**base, "survey_types": []})
            assert r.status_code == 422
            # bad code shape rejected
            r = await c.post("/api/rfqs",
                             json={**base, "survey_types": ["No Way!"]})
            assert r.status_code == 422
            # duplicates collapsed
            r = await c.post("/api/rfqs",
                             json={**base, "survey_types": ["condition", "condition"]})
            assert r.status_code == 201, r.text
            g = await c.get(f"/api/rfqs/{r.json()['id']}")
            assert g.json()["survey_types"] == ["condition"]

    client.run(scenario())
