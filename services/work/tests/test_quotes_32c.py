"""32c: the quote document — per-tenant numbering, Pak Marine echo +
snapshot fields, org-default terms/bank, draft edit, the human
approve+issue gate, revisions, and the permission matrix.

Identity is stubbed at the work.spine boundary (caller_org /
caller_member / resolve_public_org / create_identity_org /
get_identity_org), the same seam pattern as test_rfq_channels_32b.py —
with a mutable org profile per org id so the snapshot-immutability test
can move identity's answers after the fact. Rows persist in the dev
seavix_work DB, so every test uses fresh org ids.
"""
import asyncio
import os
import re
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

FIXED_UID = str(uuid.uuid4())

TENANT_PROFILE = {
    "name": "Pak Marine Services",
    "slug": "pak-marine",
    "type": "survey_company",
    "quote_prefix": "PMS",
    "default_terms": ["Org term one.", "Org term two."],
    "payment_details": {"bank": "HBL", "account_title": "Pak Marine",
                        "iban": "PK00HBL123"},
}

CLIENT_PROFILE = {
    "name": "Harbor Shipmanagement",
    "slug": "harbor",
    "type": "client",
    "contact_person": "Capt. Amin Dar",
    "designation": "Managing Director",
    "address_line1": "Pier 7",
    "address_line2": "Harbour East",
    "city": "Karachi",
    "state": "SD",
    "postal_code": "74000",
    "country_code": "PK",
    "phone_cc": "+92",
    "phone": "3012345678",
    "contact_email": "ops@harbor.example",
}

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

ITEMS = [{"description": "Attendance", "amount": 1000, "unit": "day"}]


@pytest.fixture()
def client(arun, monkeypatch):
    state = {"org_id": None, "member": None, "orgs": {}}
    created_orgs = []
    orgs = {"tenant": str(uuid.uuid4()), "client": str(uuid.uuid4()),
            "other": str(uuid.uuid4())}
    state["org_id"] = orgs["tenant"]
    state["orgs"][orgs["tenant"]] = dict(TENANT_PROFILE, id=orgs["tenant"])
    state["orgs"][orgs["client"]] = dict(CLIENT_PROFILE, id=orgs["client"])
    state["member"] = {
        "org_id": orgs["tenant"], "slug": "stub", "type": "stub",
        "uid": FIXED_UID, "email": "ops@example.com",
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
        state["orgs"][org_id] = {
            "id": org_id, "name": payload["name"], "type": "client",
            "slug": "stub", "contact_email": payload.get("contact_email")
            or payload["email"],
            "contact_person": payload.get("contact_person"),
            "designation": payload.get("designation"),
        }
        return {"org_id": org_id, "slug": "stub", "type": "client",
                "contact_email": payload["email"]}

    async def fake_get_identity_org(org_id: str) -> dict:
        # mutable per-test answers — snapshot immutability relies on this
        return state["orgs"][org_id]

    monkeypatch.setattr(spine, "caller_org", fake_caller_org)
    monkeypatch.setattr(spine, "caller_member", fake_caller_member)
    monkeypatch.setattr(spine, "resolve_public_org", fake_resolve)
    monkeypatch.setattr(spine, "get_identity_org", fake_get_identity_org)
    monkeypatch.setattr(spine, "create_identity_org", fake_create_identity_org)
    received = []
    for topic in ("rfq.created", "quote.created", "quote.sent",
                  "quote.approved"):
        app.state.event_bus.subscribe(topic, received.append)
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://work")

    class Handle:
        def __init__(self):
            self.c = c
            self.state = state
            self.orgs = orgs
            self.events = received
            self.created_orgs = created_orgs
            self.bus = app.state.event_bus
            self.run = arun

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            await self.c.aclose()

    return Handle()


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


async def _manual_rfq(c, client, **over):
    body = {**MANUAL}
    if "client_id" not in over:
        body["new_client"] = {
            "name": f"Client {uuid.uuid4().hex[:8]}",
            "email": f"{uuid.uuid4().hex[:8]}@example.com",
            "relationship_type": "management",
        }
    body.update(over)
    r = await c.post("/api/rfqs/manual", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _quote(c, rfq_id, **body):
    return await c.post(f"/api/rfqs/{rfq_id}/quotes", json=body)


# ---------------------------------------------------------------- intake

def test_raise_quote_on_public_page_rfq(client):
    async def scenario():
        async with client as c:
            TENANT = client.orgs["tenant"]
            r = await c.post("/api/public/rfqs", json={
                "org_slug": "live-surveyor", "contact_email": "b@example.com",
                "survey_types": ["pre_purchase"], "location_type": "STS",
                "vessel_imo": "9404572", "vessel_name": "MV Test",
                "reference_no": "REF-1", "port_name": "Jebel Ali",
                "scope_notes": "Pre-purchase survey",
            })
            assert r.status_code == 201, r.text
            rfq_id = r.json()["id"]
            q = await _quote(c, rfq_id, line_items=ITEMS)
            assert q.status_code == 201, q.text
            body = q.json()
            # echoes
            assert body["reference_no"] == "REF-1"
            assert body["vessel_name"] == "MV Test"
            assert body["vessel_imo"] == "9404572"
            assert body["location_type"] == "STS"
            assert body["port_name"] == "Jebel Ali"
            assert body["scope_notes"] == "Pre-purchase survey"
            assert body["survey_types"] == ["pre_purchase"]
            # anonymous public intake: only the contact email is known
            assert body["client_email"] == "b@example.com"
            assert body["client_name"] is None
            assert body["status"] == "draft"
            assert body["rfq_id"] == rfq_id

    client.run(scenario())


def test_raise_quote_on_manual_rfq_with_linked_client(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT = client.orgs["tenant"], client.orgs["client"]
            edge_id = await _make_edge(TENANT, CLIENT)
            rfq_id = await _manual_rfq(c, client, client_id=edge_id)
            q = await _quote(c, rfq_id, line_items=ITEMS)
            assert q.status_code == 201, q.text
            body = q.json()
            # the client snapshot comes from the directory edge's org
            assert body["client_name"] == "Harbor Shipmanagement"
            assert body["client_contact_person"] == "Capt. Amin Dar"
            assert body["client_designation"] == "Managing Director"
            assert body["client_country"] == "PK"
            assert body["client_phone"] == "+923012345678"
            assert body["client_email"] == "ops@harbor.example"
            assert body["client_address"] == {
                "address_line1": "Pier 7", "address_line2": "Harbour East",
                "city": "Karachi", "state": "SD", "postal_code": "74000",
                "country_code": "PK",
            }

    client.run(scenario())


def test_raise_quote_on_manual_rfq_with_quick_created_client(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client, channel="manual_email",
                                       new_client={
                "name": "Vanguard Tech", "email": "asif@example.com",
                "relationship_type": "management",
            })
            q = await _quote(c, rfq_id, line_items=ITEMS)
            assert q.status_code == 201, q.text
            assert q.json()["client_name"] == "Vanguard Tech"
            assert q.json()["client_email"] == "asif@example.com"

    client.run(scenario())


# ------------------------------------------------------------- guarding

def test_quote_requires_valid_tenant_rfq(client):
    async def scenario():
        async with client as c:
            # bogus rfq id -> plain 404
            r = await _quote(c, str(uuid.uuid4()), line_items=ITEMS)
            assert r.status_code == 404
            # another tenant's RFQ: the spine org-involvement rule is 403
            OTHER = client.orgs["other"]
            client.state["org_id"] = OTHER
            rfq_id = await _manual_rfq(c, client)
            client.state["org_id"] = client.orgs["tenant"]
            r = await _quote(c, rfq_id, line_items=ITEMS)
            assert r.status_code == 403

    client.run(scenario())


def test_cross_tenant_quote_reads_are_plain_404(client):
    async def scenario():
        async with client as c:
            TENANT, OTHER = client.orgs["tenant"], client.orgs["other"]
            rfq_id = await _manual_rfq(c, client)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            quote_id = q["id"]
            client.state["org_id"] = OTHER
            client.state["member"] = {**client.state["member"],
                                      "org_id": OTHER}
            assert (await c.get(f"/api/quotes/{quote_id}")).status_code == 404
            assert (await c.put(f"/api/quotes/{quote_id}",
                                json={"currency": "EUR"})).status_code == 404
            assert (await c.post(
                f"/api/quotes/{quote_id}/approve-issue",
                json={"confirm": True})).status_code == 404
            assert (await c.post(
                f"/api/quotes/{quote_id}/revision")).status_code == 404
            # the other tenant's list is empty and hides the quote entirely
            listing = (await c.get("/api/quotes")).json()
            assert all(x["id"] != quote_id for x in listing["quotes"])

    client.run(scenario())


# ------------------------------------------------------------ round-trip

def test_pak_marine_fields_round_trip(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q = await _quote(c, rfq_id, line_items=ITEMS)
            assert q.status_code == 201, q.text
            body = q.json()
            # org profile prefill (body omitted terms/bank_details)
            assert body["terms"] == ["Org term one.", "Org term two."]
            assert body["bank_details"] == TENANT_PROFILE["payment_details"]
            # currency default + caller stamp
            assert body["currency"] == "USD"
            assert body["prepared_by"] == FIXED_UID
            assert body["prepared_by_name"] == "ops@example.com"
            assert body["prepared_by_email"] == "ops@example.com"
            # numbering format PREFIX-NN-YYYY
            assert re.fullmatch(r"PMS-\d{2}-2026", body["quote_no"]), \
                body["quote_no"]
            assert body["quote_no"].endswith(f"-{body['quote_date'][:4]}")
            # everything the web detail needs is there
            for key in ("id", "rfq_id", "revision", "status", "quote_no",
                        "quote_date", "tenant_org_id", "valid_until",
                        "total", "created_at", "updated_at"):
                assert key in body, key
            assert body["tenant_org_id"] == client.orgs["tenant"]
            # the stored row reads back identically via detail + list
            g = await c.get(f"/api/quotes/{body['id']}")
            assert g.status_code == 200
            assert g.json() == body
            listing = (await c.get("/api/quotes")).json()
            mine = [x for x in listing["quotes"] if x["id"] == body["id"]]
            assert mine and mine[0]["quote_no"] == body["quote_no"]

    client.run(scenario())


def test_snapshot_immutability(client):
    async def scenario():
        async with client as c:
            TENANT, CLIENT = client.orgs["tenant"], client.orgs["client"]
            edge_id = await _make_edge(TENANT, CLIENT)
            rfq_id = await _manual_rfq(c, client, client_id=edge_id)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            # identity's answers MOVE after the quote exists
            client.state["orgs"][CLIENT]["contact_person"] = "Someone Else"
            client.state["orgs"][CLIENT]["designation"] = "Intern"
            client.state["orgs"][TENANT]["default_terms"] = ["Changed."]
            g = (await c.get(f"/api/quotes/{q['id']}")).json()
            assert g["client_contact_person"] == "Capt. Amin Dar"
            assert g["client_designation"] == "Managing Director"
            assert g["terms"] == ["Org term one.", "Org term two."]

    client.run(scenario())


# ------------------------------------------------------------- numbering

def test_per_tenant_numbering_and_prefix_fallback(client):
    async def scenario():
        async with client as c:
            TENANT, OTHER, CLIENT = (client.orgs["tenant"],
                                     client.orgs["other"],
                                     client.orgs["client"])
            rfq_a = await _manual_rfq(c, client)
            q1 = (await _quote(c, rfq_a, line_items=ITEMS)).json()
            q2 = (await _quote(c, rfq_a, line_items=ITEMS)).json()
            assert q1["quote_no"] == "PMS-01-2026"
            assert q2["quote_no"] == "PMS-02-2026"
            assert q2["revision"] == 2

            # second tenant: independent sequence + slug-derived prefix
            # (its profile has no quote_prefix)
            client.state["orgs"][OTHER] = {
                "id": OTHER, "name": "Gulf Marine", "slug": "gulf-marine",
                "type": "survey_company",
            }
            client.state["org_id"] = OTHER
            client.state["member"] = {**client.state["member"],
                                      "org_id": OTHER}
            client2 = str(uuid.uuid4())
            client.state["orgs"][client2] = dict(CLIENT_PROFILE,
                                                 id=client2)
            edge2 = await _make_edge(OTHER, client2)
            rfq_b = await _manual_rfq(c, client, client_id=edge2)
            q3 = (await _quote(c, rfq_b, line_items=ITEMS)).json()
            assert q3["quote_no"] == "GULF-MARINE-01-2026"

            # year rollover: the sequence is keyed by (tenant, year)
            q4 = (await _quote(c, rfq_b, line_items=ITEMS,
                               quote_date="2027-01-15")).json()
            assert q4["quote_no"] == "GULF-MARINE-01-2027"

    client.run(scenario())


def test_concurrent_creates_get_unique_numbers(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            results = await asyncio.gather(*[
                _quote(c, rfq_id, line_items=ITEMS) for _ in range(8)
            ])
            assert all(r.status_code == 201 for r in results)
            numbers = [r.json()["quote_no"] for r in results]
            assert len(set(numbers)) == 8  # no duplicates
            seqs = sorted(int(n.split("-")[1]) for n in numbers)
            assert seqs == list(range(1, 9))  # no gaps
            revisions = sorted(r.json()["revision"] for r in results)
            assert revisions == list(range(1, 9))

    client.run(scenario())


# ----------------------------------------------------------- line items

def test_line_item_validation_matrix(client):
    async def scenario():
        async with client as c:
            good = [{"description": "Attendance", "amount": 1000,
                     "unit": "day"}]
            cases = [
                ([{"description": "Free", "amount": 0}], 201),   # zero ok
                ([{"description": "X", "amount": -1}], 422),     # negative
                ([{"description": "X", "amount": "abc"}], 422),  # unparseable
                ([{"description": "X", "amount": "1.234"}], 422),  # >2dp
                ([{"description": "X", "amount": True}], 422),   # bool
                ([{"description": "X"}], 422),                   # missing
                ([{"description": "  ", "amount": 1}], 422),     # empty desc
                (["nope"], 422),                                 # not objects
                ("nope", 422),                                   # not a list
            ]
            for i, (items, expected) in enumerate(cases):
                rfq_id = await _manual_rfq(c, client)
                r = await _quote(c, rfq_id, line_items=items)
                assert r.status_code == expected, (i, r.text)

            # empty list is allowed on a draft; total is server-side
            rfq_id = await _manual_rfq(c, client)
            r = await _quote(c, rfq_id, line_items=[])
            assert r.status_code == 201, r.text
            assert r.json()["line_items"] == []
            assert r.json()["total"] == "0.00"

            # mixed int/str amounts sum correctly; extra keys verbatim
            rfq_id = await _manual_rfq(c, client)
            mixed = [{"description": "A", "amount": 1000, "unit": "day"},
                     {"description": "B", "amount": "950.50"},
                     {"description": "C", "amount": 49.5, "unit": "lump"}]
            r = await _quote(c, rfq_id, line_items=mixed)
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["line_items"] == mixed
            assert body["total"] == "2000.00"
            # zero-amount total
            rfq_id = await _manual_rfq(c, client)
            r = await _quote(c, rfq_id,
                             line_items=[{"description": "Z", "amount": 0}])
            assert r.json()["total"] == "0.00"

    client.run(scenario())


def test_draft_line_items_add_remove_via_put(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            two = ITEMS + [{"description": "Travel", "amount": 250,
                            "unit": "trip"}]
            r = await c.put(f"/api/quotes/{q['id']}", json={"line_items": two})
            assert r.status_code == 200, r.text
            assert r.json()["line_items"] == two
            assert r.json()["total"] == "1250.00"
            one = [two[1]]
            r = await c.put(f"/api/quotes/{q['id']}", json={"line_items": one})
            assert r.status_code == 200, r.text
            assert r.json()["line_items"] == one
            assert r.json()["total"] == "250.00"
            # invalid edits rejected, stored rows untouched
            r = await c.put(f"/api/quotes/{q['id']}",
                            json={"line_items": [{"description": "X",
                                                  "amount": -5}]})
            assert r.status_code == 422

    client.run(scenario())


# ------------------------------------------------- terms / bank defaults

def test_terms_and_bank_defaults_and_overrides(client):
    async def scenario():
        async with client as c:
            # body-provided terms/bank win over the org profile
            rfq_id = await _manual_rfq(c, client)
            r = await _quote(c, rfq_id, line_items=ITEMS,
                             terms=["Custom term."],
                             bank_details={"bank": "Askari"})
            body = r.json()
            assert body["terms"] == ["Custom term."]
            assert body["bank_details"] == {"bank": "Askari"}
            # terms are stripped/empties dropped
            r = await _quote(c, rfq_id, line_items=ITEMS,
                             terms=["  A  ", "", "   "])
            assert r.json()["terms"] == ["A"]

            # org without a profile falls back to the 9 Pak Marine terms
            OTHER = client.orgs["other"]
            client.state["org_id"] = OTHER
            client.state["member"] = {**client.state["member"],
                                      "org_id": OTHER}
            client2 = str(uuid.uuid4())
            client.state["orgs"][client2] = dict(CLIENT_PROFILE,
                                                 id=client2)
            edge2 = await _make_edge(OTHER, client2)
            rfq_b = await _manual_rfq(c, client, client_id=edge2)
            r = await _quote(c, rfq_b, line_items=ITEMS)
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["terms"] == spine.DEFAULT_TERMS
            assert len(body["terms"]) == 9
            assert body["bank_details"] is None

            # per-quote edits round-trip via PUT (bank cleared only when
            # explicitly re-sent — None means "not provided")
            r = await c.put(f"/api/quotes/{body['id']}",
                            json={"terms": ["Edited."],
                                  "bank_details": {"bank": "Edited"}})
            assert r.status_code == 200, r.text
            assert r.json()["terms"] == ["Edited."]
            assert r.json()["bank_details"] == {"bank": "Edited"}

    client.run(scenario())


# ---------------------------------------------------------- draft / issue

def test_draft_never_advances_rfq_until_issue(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            g = (await c.get(f"/api/rfqs/{rfq_id}")).json()
            assert g["status"] == "awaiting_quote"
            # the embedded quote carries the document fields too
            assert g["quotes"][0]["quote_no"] == q["quote_no"]
            assert g["quotes"][0]["total"] == "1000.00"
            r = await c.put(f"/api/quotes/{q['id']}",
                            json={"currency": "EUR"})
            assert r.status_code == 200, r.text
            assert r.json()["currency"] == "EUR"
            g = (await c.get(f"/api/rfqs/{rfq_id}")).json()
            assert g["status"] == "awaiting_quote"
            # the gate flips it
            r = await c.post(f"/api/quotes/{q['id']}/approve-issue",
                             json={"confirm": True})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "sent"
            assert (await c.get(f"/api/rfqs/{rfq_id}")).json()["status"] \
                == "quoted"

    client.run(scenario())


def test_approve_issue_gate(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            quote_id = q["id"]
            # anything but {"confirm": true} is a 422
            assert (await c.post(f"/api/quotes/{quote_id}/approve-issue",
                                 json={"confirm": False})).status_code == 422
            assert (await c.post(f"/api/quotes/{quote_id}/approve-issue",
                                 json={})).status_code == 422

            before = len(client.bus.published)
            r = await c.post(f"/api/quotes/{quote_id}/approve-issue",
                             json={"confirm": True})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "sent"
            # the caller's stamp is recorded on all four gate fields
            assert body["approved_by"] == FIXED_UID
            assert body["issued_by"] == FIXED_UID
            assert body["approved_at"] is not None
            assert body["issued_at"] is not None

            # exactly one approved + one sent event, both streams, in order
            new_events = [e for _, e in client.bus.published[before:]]
            flow = [e for e in new_events
                    if e["type"] in ("quote.approved", "quote.sent")]
            assert [e["type"] for e in flow] == ["quote.approved",
                                                 "quote.sent"]
            assert all(e["visibility"] == "both" for e in flow)
            assert flow[0]["payload"]["approved_by"] == FIXED_UID

            # no OTP/email side channels fire during approval
            assert not any("email" in e["type"] or "otp" in e["type"]
                           for e in new_events)

            # a sent document is immutable and cannot re-issue
            assert (await c.post(f"/api/quotes/{quote_id}/approve-issue",
                                 json={"confirm": True})).status_code == 409
            assert (await c.put(f"/api/quotes/{quote_id}",
                                json={"currency": "EUR"})).status_code == 409

    client.run(scenario())


def test_revision_flow(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q1 = (await _quote(c, rfq_id, line_items=ITEMS,
                               terms=["Rev one."])).json()
            r = await c.post(f"/api/quotes/{q1['id']}/approve-issue",
                             json={"confirm": True})
            assert r.status_code == 200, r.text

            r = await c.post(f"/api/quotes/{q1['id']}/revision")
            assert r.status_code == 201, r.text
            q2 = r.json()
            assert q2["rfq_id"] == rfq_id
            assert q2["revision"] == 2
            assert q2["status"] == "draft"
            assert q2["quote_no"] == "PMS-02-2026"   # fresh number
            assert q2["line_items"] == ITEMS         # copied verbatim
            assert q2["terms"] == ["Rev one."]
            assert q2["approved_by"] is None         # gate fields reset
            assert q2["approved_at"] is None
            assert q2["prepared_by"] == FIXED_UID

            # the issued rev1 is superseded; the RFQ stays quoted
            g = (await c.get(f"/api/rfqs/{rfq_id}")).json()
            assert g["status"] == "quoted"
            by_rev = {x["revision"]: x for x in g["quotes"]}
            assert by_rev[1]["status"] == "superseded"
            assert by_rev[2]["status"] == "draft"

            # the new revision can be issued in its own right
            r = await c.post(f"/api/quotes/{q2['id']}/approve-issue",
                             json={"confirm": True})
            assert r.status_code == 200, r.text
            assert (await c.get(f"/api/rfqs/{rfq_id}")).json()["status"] \
                == "quoted"

    client.run(scenario())


# --------------------------------------------------------------- listing

def test_quote_list_filters_and_quote_options(client):
    async def scenario():
        async with client as c:
            TENANT, OTHER, CLIENT = (client.orgs["tenant"],
                                     client.orgs["other"],
                                     client.orgs["client"])
            edge_id = await _make_edge(TENANT, CLIENT)
            rfq_1 = await _manual_rfq(c, client, client_id=edge_id)
            q1 = (await _quote(c, rfq_1, line_items=ITEMS)).json()
            r = await c.post(f"/api/quotes/{q1['id']}/approve-issue",
                             json={"confirm": True})
            assert r.status_code == 200, r.text
            rfq_2 = await _manual_rfq(c, client, client_id=edge_id,
                                      reference_no="UNIQUE-REF-77")
            q2 = (await _quote(c, rfq_2, line_items=ITEMS)).json()

            # status filter
            drafts = (await c.get("/api/quotes",
                                  params={"status": "draft"})).json()
            assert any(x["id"] == q2["id"] for x in drafts["quotes"])
            assert all(x["status"] == "draft" for x in drafts["quotes"])
            sent = (await c.get("/api/quotes",
                                params={"status": "sent"})).json()
            assert any(x["id"] == q1["id"] for x in sent["quotes"])
            # q filter across quote_no / client_name / vessel / reference
            by_client = (await c.get("/api/quotes",
                                     params={"q": "Harbor"})).json()
            assert {x["id"] for x in by_client["quotes"]} == {q1["id"],
                                                              q2["id"]}
            by_no = (await c.get("/api/quotes",
                                 params={"q": q1["quote_no"]})).json()
            assert [x["id"] for x in by_no["quotes"]] == [q1["id"]]
            none = (await c.get("/api/quotes",
                                params={"q": "zzz-no-match"})).json()
            assert none["quotes"] == []

            # quote-options: only awaiting_quote/quoted targeted at caller
            opts = (await c.get("/api/rfqs/quote-options")).json()
            ids = {x["id"] for x in opts["rfqs"]}
            assert rfq_1 in ids          # quoted
            assert rfq_2 in ids          # awaiting_quote
            assert all(x["status"] in ("awaiting_quote", "quoted")
                       for x in opts["rfqs"])
            assert all(x["client"] in ("Harbor Shipmanagement", None)
                       for x in opts["rfqs"])
            # q filter hits reference_no
            filtered = (await c.get("/api/rfqs/quote-options",
                                    params={"q": "UNIQUE-REF"})).json()
            assert [x["id"] for x in filtered["rfqs"]] == [rfq_2]
            # id prefix match works too
            by_id = (await c.get("/api/rfqs/quote-options",
                                 params={"q": rfq_1[:8]})).json()
            assert rfq_1 in {x["id"] for x in by_id["rfqs"]}

            # another tenant's RFQ never shows up
            client.state["org_id"] = OTHER
            client.state["member"] = {**client.state["member"],
                                      "org_id": OTHER}
            opts_other = (await c.get("/api/rfqs/quote-options")).json()
            assert rfq_1 not in {x["id"] for x in opts_other["rfqs"]}
            assert rfq_2 not in {x["id"] for x in opts_other["rfqs"]}

    client.run(scenario())


# ------------------------------------------------------------ role matrix

def test_role_matrix_permissions(client):
    async def scenario():
        async with client as c:
            rfq_id = await _manual_rfq(c, client)
            q = (await _quote(c, rfq_id, line_items=ITEMS)).json()
            quote_id = q["id"]

            # minus quote.create: every write path is a plain 404
            base = {"quote.view", "quote.send", "quote.accept"}
            client.state["member"] = {**client.state["member"],
                                      "permissions": set(base)}
            assert (await _quote(c, rfq_id, line_items=ITEMS)) \
                .status_code == 404
            assert (await c.put(f"/api/quotes/{quote_id}",
                                json={"currency": "EUR"})) \
                .status_code == 404
            assert (await c.post(f"/api/quotes/{quote_id}/approve-issue",
                                 json={"confirm": True})).status_code == 404
            assert (await c.post(f"/api/quotes/{quote_id}/revision")) \
                .status_code == 404
            assert (await c.get("/api/rfqs/quote-options")).status_code == 404

            # minus quote.view: reads are a plain 404, writes still fine
            client.state["member"] = {
                **client.state["member"],
                "permissions": {"quote.create", "quote.send",
                                "quote.accept"},
            }
            assert (await c.get("/api/quotes")).status_code == 404
            assert (await c.get(f"/api/quotes/{quote_id}")) \
                .status_code == 404
            assert (await _quote(c, rfq_id, line_items=ITEMS)) \
                .status_code == 201

    client.run(scenario())
