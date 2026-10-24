"""32a: client directory endpoints — org-scoped, permission-gated
(404-over-403), the org-before-edge write order, and identity failure ->
502 with NO directory row written.

Identity is stubbed at the work.clients boundary (require_permission /
create_identity_org / get_identity_org), the same seam pattern as
test_work.py; rows persist in the dev seavix_work DB, so every test uses a
fresh tenant org id.
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

import work.clients as clients_mod  # noqa: E402
from work.main import app  # noqa: E402

OPS_PERMS = {
    "org.profile.view", "rfq.create", "rfq.view", "quote.create",
    "quote.view", "quote.send", "quote.accept", "job.view", "job.assign",
    "client.view", "client.create", "client.edit",
}
SURVEYOR_PERMS = {"rfq.view", "job.view"}
CLIENT_ROLE_PERMS = {"rfq.create", "rfq.view"}

FORM = {
    "name": "Harbor Shipmanagement",
    "relationship_type": "management",
    "email": f"desk-{uuid.uuid4().hex[:8]}@example.com",
    "contact_email": "ops@harbor.example",
    "phone_cc": "+971",
    "phone": "50 123 4567",
    "whatsapp_cc": "+971",
    "whatsapp": "50 765 4321",
    "address_line1": "Pier 7",
    "city": "Dubai",
    "state": "DU",
    "postal_code": "12345",
    "country_code": "AE",
}


@pytest.fixture()
def harness(arun, monkeypatch):
    state = {
        "org_id": str(uuid.uuid4()),
        "permissions": sorted(OPS_PERMS),
        "orgs": {},            # org_id -> identity org dict
        "identity_calls": [],
        "fail_identity": False,
        "fixed_org_id": None,  # duplicate-edge simulation
    }

    async def fake_require_permission(request, permission):
        if permission not in state["permissions"]:
            raise HTTPException(status_code=404, detail="not found")
        return {"org_id": state["org_id"],
                "permissions": state["permissions"]}

    async def fake_create_identity_org(payload):
        if state["fail_identity"]:
            raise HTTPException(status_code=502,
                                detail="identity service error")
        org_id = state["fixed_org_id"] or str(uuid.uuid4())
        org = {
            "id": org_id,
            "name": payload["name"],
            "type": "client",
            "slug": "stub",
            "contact_email": payload.get("contact_email") or payload["email"],
            "phone": payload.get("phone"),
            "phone_cc": payload.get("phone_cc"),
            "whatsapp": payload.get("whatsapp"),
            "whatsapp_cc": payload.get("whatsapp_cc"),
            "address_line1": payload.get("address_line1"),
            "address_line2": payload.get("address_line2"),
            "city": payload.get("city"),
            "state": payload.get("state"),
            "postal_code": payload.get("postal_code"),
            "country_code": payload.get("country_code"),
        }
        state["orgs"][org_id] = org
        state["identity_calls"].append(payload)
        return {"org_id": org_id, "slug": "stub", "type": "client",
                "contact_email": org["contact_email"]}

    async def fake_get_identity_org(org_id):
        if org_id not in state["orgs"]:
            raise HTTPException(status_code=502,
                                detail="identity service error")
        return state["orgs"][org_id]

    monkeypatch.setattr(clients_mod, "require_permission",
                        fake_require_permission)
    monkeypatch.setattr(clients_mod, "create_identity_org",
                        fake_create_identity_org)
    monkeypatch.setattr(clients_mod, "get_identity_org", fake_get_identity_org)

    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://work")

    class Handle:
        def __init__(self):
            self.c = c
            self.state = state
            self.run = arun

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            await self.c.aclose()

    return Handle()


def test_ops_create_list_view_round_trip(harness):
    async def scenario():
        async with harness as c:
            r = await c.post("/api/clients", json=FORM)
            assert r.status_code == 201, r.text
            client_id = r.json()["id"]
            assert r.json()["relationship_type"] == "management"

            # the full contact block reached identity
            assert harness.state["identity_calls"][0]["name"] == FORM["name"]
            assert harness.state["identity_calls"][0]["phone_cc"] == "+971"
            assert harness.state["identity_calls"][0]["country_code"] == "AE"

            # list: org-scoped, enriched with the org + relationship badge
            g = await c.get("/api/clients")
            assert g.status_code == 200
            clients = g.json()["clients"]
            mine = [cl for cl in clients if cl["id"] == client_id]
            assert len(mine) == 1
            entry = mine[0]
            assert entry["relationship_type"] == "management"
            assert entry["org"]["name"] == "Harbor Shipmanagement"
            assert entry["org"]["contact_email"] == "ops@harbor.example"
            assert entry["org"]["city"] == "Dubai"
            assert entry["org"]["country_code"] == "AE"

            # detail renders the contact fields
            d = await c.get(f"/api/clients/{client_id}")
            assert d.status_code == 200
            assert d.json()["org"]["phone_cc"] == "+971"
            assert d.json()["org"]["phone"] == "50 123 4567"
            assert d.json()["org"]["whatsapp"] == "50 765 4321"
            assert d.json()["org"]["address_line1"] == "Pier 7"
            assert d.json()["org"]["postal_code"] == "12345"

            # unknown id / another tenant's id -> 404
            assert (await c.get(f"/api/clients/{uuid.uuid4()}")).status_code == 404
            harness.state["org_id"] = str(uuid.uuid4())
            assert (await c.get(f"/api/clients/{client_id}")).status_code == 404

    harness.run(scenario())


def test_unknown_relationship_type_422(harness):
    async def scenario():
        async with harness as c:
            r = await c.post("/api/clients",
                             json={**FORM, "relationship_type": "broker"})
            assert r.status_code == 422, r.text

    harness.run(scenario())


def test_duplicate_edge_409(harness):
    async def scenario():
        async with harness as c:
            harness.state["fixed_org_id"] = str(uuid.uuid4())
            assert (await c.post("/api/clients", json=FORM)).status_code == 201
            r = await c.post("/api/clients", json={**FORM, "name": "Other"})
            assert r.status_code == 409, r.text

    harness.run(scenario())


def test_identity_failure_writes_no_edge(harness):
    async def scenario():
        async with harness as c:
            harness.state["fail_identity"] = True
            r = await c.post("/api/clients", json=FORM)
            assert r.status_code == 502, r.text
            g = await c.get("/api/clients")
            assert g.status_code == 200
            assert g.json()["clients"] == []

    harness.run(scenario())


@pytest.mark.parametrize("perms", [sorted(SURVEYOR_PERMS),
                                   sorted(CLIENT_ROLE_PERMS)])
def test_permissionless_roles_get_404(harness, perms):
    async def scenario():
        async with harness as c:
            harness.state["permissions"] = perms
            assert (await c.get("/api/clients")).status_code == 404
            assert (await c.get(f"/api/clients/{uuid.uuid4()}")).status_code == 404
            assert (await c.post("/api/clients", json=FORM)).status_code == 404

    harness.run(scenario())
