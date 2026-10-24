"""32d: custom catalog entries + harvest — identity-owned catalogs only
(service, vessel_type, survey_type; PORTS EXCLUDED — UN/LOCODE is
registry-owned external reference data, never created here).

Covers: create persists origin='custom' + created_by_org_id provenance;
duplicate label (any origin, case-insensitive) SELECTS the existing entry
instead of creating; label validation (non-empty, <=60, trimmed); the
harvest — a custom entry appears in EVERY org's search, ranked after
system rows, with origin surfaced for the dropdown badge; survey_type
catalog seeds; profile survey_types round-trip + unknown-code 422; the
anonymous public search/create pair (NULL provenance).
"""
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register_login(c, email):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Catalog Org",
            "org_type": "survey_company",
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


def test_create_custom_entry_with_provenance_and_harvest(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            label = f"Underwater welding {u}"
            sid_a = await _register_login(c, f"cat-a-{u}@example.com")
            sid_b = await _register_login(c, f"cat-b-{u}@example.com")
            auth_a = {"X-Seavix-Session": sid_a}
            auth_b = {"X-Seavix-Session": sid_b}

            # create from org A
            r = await c.post(
                "/api/catalog/services", json={"label": f"  {label}  "},
                headers=auth_a,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["label"] == label  # trimmed
            assert body["origin"] == "custom"
            code = body["code"]
            assert code != label and code.startswith("underwater_welding_")
            assert len(code) <= 64

            # provenance in the DB
            from sqlalchemy import text as sql

            from app.db import SessionLocal

            org_id = (
                await c.get("/api/internal/auth-context", headers=auth_a)
            ).json()["memberships"][0]["org_id"]
            async with SessionLocal() as s:
                row = (await s.execute(
                    sql("SELECT origin, created_by_org_id, created_at "
                        "FROM service WHERE service_code = :code"),
                    {"code": code})).mappings().one()
                assert row["origin"] == "custom"
                assert str(row["created_by_org_id"]) == org_id
                assert row["created_at"] is not None

            # HARVEST: org B's search surfaces it (query the FULL unique
            # label — the shared dev DB accumulates rows from repeated
            # runs, and search is LIMITed), system rows first
            g = await c.get(
                "/api/catalog/services/search", params={"q": label},
                headers=auth_b,
            )
            entries = [e for e in g.json()["services"]
                       if e["label"] == label]
            assert len(entries) == 1 and entries[0]["origin"] == "custom"
            results = g.json()["services"]
            origins = [e["origin"] for e in results]
            assert origins == sorted(origins, key=lambda o: o != "system"), origins

            # duplicate label (other org, different case) SELECTS existing
            dup = await c.post(
                "/api/catalog/services", json={"label": label.upper()},
                headers=auth_b,
            )
            assert dup.status_code == 200, dup.text
            assert dup.json()["code"] == code
            assert dup.json()["origin"] == "custom"
            async with SessionLocal() as s:
                count = (await s.execute(
                    sql("SELECT count(*) FROM service WHERE label = :label"),
                    {"label": label})).scalar_one()
                assert count == 1

            # validation
            assert (await c.post(
                "/api/catalog/services", json={"label": ""}, headers=auth_a
            )).status_code == 422
            assert (await c.post(
                "/api/catalog/services", json={"label": "x" * 61}, headers=auth_a
            )).status_code == 422
            # unknown catalog kind
            assert (await c.post(
                "/api/catalog/ports", json={"label": "Dubai"}, headers=auth_a
            )).status_code == 404

    arun(scenario())


def test_survey_type_seeds_and_profile_round_trip(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"cat-st-{u}@example.com"
            sid = await _register_login(c, email)
            auth = {"X-Seavix-Session": sid}

            g = await c.get("/api/catalog/survey-types/search", headers=auth)
            seeds = {e["code"]: e for e in g.json()["survey_types"]
                     if e["code"] in ("pre_purchase", "condition",
                                      "bunker_quantity", "loading_discharging")}
            assert set(seeds) == {"pre_purchase", "condition",
                                  "bunker_quantity", "loading_discharging"}
            assert all(e["origin"] == "system" for e in seeds.values())

            # custom survey type + profile round trip
            made = await c.post(
                "/api/catalog/survey-types", json={"label": f"Warranty {u}"},
                headers=auth,
            )
            assert made.status_code == 201, made.text
            custom_code = made.json()["code"]

            put = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={"survey_types": ["pre_purchase", custom_code]},
            )
            assert put.status_code == 200, put.text

            me_slug = (
                await c.get("/api/internal/auth-context", headers=auth)
            ).json()["memberships"][0]["slug"]
            pub = await c.get(f"/api/orgs/{me_slug}")
            resolved = {e["code"]: e["label"]
                        for e in pub.json()["survey_types"]}
            assert resolved["pre_purchase"] == "Pre-purchase survey"
            assert resolved[custom_code] == f"Warranty {u}"

            # unknown survey type code -> 422 naming the field
            bad = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"survey_types": ["not_a_real_type_zz"]},
            )
            assert bad.status_code == 422, bad.text
            assert "survey type" in str(bad.json()["detail"])

    arun(scenario())


def test_public_survey_type_catalog_anonymous(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            label = f"Public harvest {u}"
            # anonymous search sees system seeds
            g = await c.get("/api/public/catalog/survey-types/search",
                            params={"q": "condition"})
            assert g.status_code == 200
            assert any(e["code"] == "condition" for e in g.json()["survey_types"])

            # anonymous create: origin custom, NULL provenance
            r = await c.post("/api/public/catalog/survey-types",
                             json={"label": label})
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["origin"] == "custom"

            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                row = (await s.execute(
                    sql("SELECT created_by_org_id FROM survey_type "
                        "WHERE survey_type_code = :code"),
                    {"code": body["code"]})).one()
                assert row[0] is None

            # it harvested: the next anonymous search finds it
            g2 = await c.get("/api/public/catalog/survey-types/search",
                             params={"q": "Public harvest"})
            assert any(e["code"] == body["code"] for e in g2.json()["survey_types"])

            # duplicate selects existing (200, same code)
            dup = await c.post("/api/public/catalog/survey-types",
                               json={"label": label})
            assert dup.status_code == 200 and dup.json()["code"] == body["code"]

    arun(scenario())
