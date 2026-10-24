"""32c slice 1 (identity side): org-level commercial defaults for quotations.

- GET/PUT /api/orgs/me/commercial — org.profile.view / org.profile.edit
  (org-account settings, NOT the public profile)
- GET /api/internal/orgs/id/{org_id} now carries quote_prefix,
  default_terms, payment_details (the work service's quote prefill)
- migration 0017: the three columns + the Pak Marine default-terms
  backfill (DEFAULT_TERMS in app/profile.py is the canonical copy)
"""
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.profile import DEFAULT_TERMS  # noqa: E402


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register_login(c, email, org_type="survey_company"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": f"Commercial Org {uuid.uuid4().hex[:6]}",
            "org_type": org_type,
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()
    start = await c.post(
        "/api/auth/login/start", json={"email": email, "password": "secret-123"}
    )
    code = (await c.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    done = await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    assert done.status_code == 200, done.text
    return done.json()["session_id"]


PAK_MARINE_TERMS = [
    "Our offer is valid for 30 days from the date of this quotation.",
    "Condition report will be submitted within 36 hours of survey completion.",
    "Pre-purchase report will be submitted within 72 hours of survey completion.",
    "Payment is due within 07 days of invoice date.",
    "Travel expenses will be charged at actuals plus traveling days.",
    "Anchorage surcharge applies as applicable.",
    "Gate pass arrangements are on client's account.",
    "50% advance payment is required before mobilization.",
    "Bank charges are on client's account.",
]


def test_default_terms_constant_matches_master_doc():
    assert DEFAULT_TERMS == PAK_MARINE_TERMS


def test_commercial_get_put_round_trip(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            sid = await _register_login(c, f"com-{u}@example.com")
            auth = {"X-Seavix-Session": sid}
            org_id = (
                await c.get("/api/internal/auth-context", headers=auth)
            ).json()["memberships"][0]["org_id"]

            # fresh org: all three NULL (only pre-migration rows were
            # backfilled — 0017 ran before this org existed)
            g = await c.get("/api/orgs/me/commercial", headers=auth)
            assert g.status_code == 200, g.text
            body = g.json()
            assert body["org_id"] == org_id
            assert body["quote_prefix"] is None
            assert body["default_terms"] is None
            assert body["payment_details"] is None

            # PUT: prefix is uppercased; terms are stripped, blanks dropped;
            # payment details stored verbatim
            terms = [
                "  Our offer is valid for 30 days.  ",
                "",
                "Payment is due within 07 days of invoice date.",
            ]
            payment = {
                "bank_name": "Emirates NBD",
                "account_name": "Pak Marine Services LLC",
                "iban": "AE070331234567890123456",
                "swift": "EBILAEAD",
            }
            p = await c.put(
                "/api/orgs/me/commercial",
                headers=auth,
                json={
                    "quote_prefix": "pmx",  # lower case -> stored upper
                    "default_terms": terms,
                    "payment_details": payment,
                },
            )
            assert p.status_code == 200, p.text
            assert p.json() == {
                "org_id": org_id,
                "quote_prefix": "PMX",
                "default_terms": [terms[0].strip(), terms[2].strip()],
                "payment_details": payment,
            }

            # GET reflects exactly
            g2 = await c.get("/api/orgs/me/commercial", headers=auth)
            assert g2.json() == p.json()

            # payment_details merges per key: null clears only that key,
            # other keys (existing and new) survive
            m = await c.put(
                "/api/orgs/me/commercial",
                headers=auth,
                json={"payment_details": {"swift": None, "branch": "Deira"}},
            )
            assert m.status_code == 200, m.text
            merged = dict(payment)
            del merged["swift"]
            merged["branch"] = "Deira"
            assert m.json()["payment_details"] == merged

            # PUT {} and all-None both leave everything untouched
            for empty in ({}, {
                "quote_prefix": None,
                "default_terms": None,
                "payment_details": None,
            }):
                r = await c.put(
                    "/api/orgs/me/commercial", headers=auth, json=empty
                )
                assert r.status_code == 200, r.text
                assert r.json() == m.json()

    arun(scenario())


def test_commercial_permission_gate(arun):
    """Deny paths: the wall 401s without credentials; a role WITHOUT
    org.profile.view (the registered-client role) gets the endpoint-level
    403 from require_permission; owner passes."""
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]

            # no credentials -> wall
            assert (await c.get("/api/orgs/me/commercial")).status_code == 401
            assert (await c.put(
                "/api/orgs/me/commercial", json={"quote_prefix": "PMX"}
            )).status_code == 401

            # client role has rfq.* only — no org.profile.view/edit
            client_sid = await _register_login(
                c, f"com-c-{u}@example.com", org_type="client"
            )
            client_auth = {"X-Seavix-Session": client_sid}
            g = await c.get("/api/orgs/me/commercial", headers=client_auth)
            assert g.status_code == 403
            assert "org.profile.view" in g.json()["detail"]
            p = await c.put(
                "/api/orgs/me/commercial",
                headers=client_auth,
                json={"quote_prefix": "PMX"},
            )
            assert p.status_code == 403
            assert "org.profile.edit" in p.json()["detail"]

            # owner (org.profile.view) passes
            owner_sid = await _register_login(c, f"com-o-{u}@example.com")
            g2 = await c.get(
                "/api/orgs/me/commercial",
                headers={"X-Seavix-Session": owner_sid},
            )
            assert g2.status_code == 200

    arun(scenario())


def test_commercial_validation(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            sid = await _register_login(c, f"com-v-{u}@example.com")
            auth = {"X-Seavix-Session": sid}
            url = "/api/orgs/me/commercial"

            # quote_prefix: 1 char too short, illegal chars
            assert (await c.put(
                url, headers=auth, json={"quote_prefix": "A"}
            )).status_code == 422
            assert (await c.put(
                url, headers=auth, json={"quote_prefix": "!!!"}
            )).status_code == 422
            assert (await c.put(
                url, headers=auth, json={"quote_prefix": "A" * 13}
            )).status_code == 422

            # payment_details: unknown key, non-string value, oversized value
            assert (await c.put(
                url, headers=auth,
                json={"payment_details": {"bitcoin": "abc"}},
            )).status_code == 422
            assert (await c.put(
                url, headers=auth,
                json={"payment_details": {"bank_name": 123}},
            )).status_code == 422
            assert (await c.put(
                url, headers=auth,
                json={"payment_details": {"bank_name": "x" * 201}},
            )).status_code == 422

            # default_terms: not a list, non-string line, >50 lines,
            # >500-char line
            assert (await c.put(
                url, headers=auth, json={"default_terms": "not a list"}
            )).status_code == 422
            assert (await c.put(
                url, headers=auth, json={"default_terms": [1, 2]}
            )).status_code == 422
            assert (await c.put(
                url, headers=auth,
                json={"default_terms": [f"line {i}" for i in range(51)]},
            )).status_code == 422
            assert (await c.put(
                url, headers=auth, json={"default_terms": ["x" * 501]}
            )).status_code == 422

            # nothing was written by any rejected PUT
            g = await c.get(url, headers=auth)
            assert g.json()["quote_prefix"] is None
            assert g.json()["default_terms"] is None
            assert g.json()["payment_details"] is None

    arun(scenario())


def test_internal_org_by_id_carries_commercial_fields(arun):
    """The work service's prefill path: /api/internal/orgs/id/{org_id}
    returns quote_prefix/default_terms/payment_details alongside the 32a
    directory fields (in-process ASGI: request.client is None, so the
    loopback guard passes)."""
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            sid = await _register_login(c, f"com-i-{u}@example.com")
            auth = {"X-Seavix-Session": sid}
            org_id = (
                await c.get("/api/internal/auth-context", headers=auth)
            ).json()["memberships"][0]["org_id"]

            terms = ["Line one.", "Line two."]
            payment = {"bank_name": "HSBC", "account_number": "001-234567"}
            p = await c.put(
                "/api/orgs/me/commercial", headers=auth,
                json={
                    "quote_prefix": "PKM",
                    "default_terms": terms,
                    "payment_details": payment,
                },
            )
            assert p.status_code == 200, p.text

            g = await c.get(f"/api/internal/orgs/id/{org_id}")
            assert g.status_code == 200, g.text
            body = g.json()
            assert body["quote_prefix"] == "PKM"
            assert body["default_terms"] == terms
            assert body["payment_details"] == payment
            # the 32a directory fields are untouched
            assert body["contact_person"] is None
            assert body["id"] == org_id

            # unknown org still 404s
            ghost = await c.get(f"/api/internal/orgs/id/{uuid.uuid4()}")
            assert ghost.status_code == 404

    arun(scenario())
