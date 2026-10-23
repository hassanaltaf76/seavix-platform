"""31q: org services + vessel types — persistence behind PUT
/api/orgs/me/profile and the public read model (GET /api/orgs/{slug}
carries them RESOLVED to [{code, label}] rows, identity-owned catalogs
joined at read time).

Unlike 31f ports (registry owns the reference data; the web app
validates existence there), these catalogs live in IDENTITY's own DB
(migration 0011: service / vessel_type + org_service / org_vessel_type),
so identity validates existence itself and the read model resolves
labels without a cross-service round trip.
"""
import sys
import uuid

import httpx

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.main import app  # noqa: E402

SERVICES = [
    ("pre_purchase_survey", "Pre-purchase survey", 1),
    ("condition_survey", "Condition survey", 2),
    ("bunker_quantity_survey", "Bunker quantity survey", 3),
    ("loading_discharging_attendance", "Loading/discharging attendance", 4),
    ("on_hire_off_hire_survey", "On-hire/off-hire survey", 5),
    ("draft_survey", "Draft survey", 6),
    ("cargo_hold_condition", "Cargo hold condition", 7),
    ("magnetic_compass_adjustment", "Magnetic compass adjustment", 8),
    ("hot_work_gas_free_certificate", "Hot work / gas-free certificate", 9),
    ("third_party_inspection", "Third-party inspection", 10),
]

VESSEL_TYPES = [
    ("oil_tanker", "Oil tanker", 1),
    ("chemical_tanker", "Chemical tanker", 2),
    ("gas_carrier", "Gas carrier", 3),
    ("bulk_carrier", "Bulk carrier", 4),
    ("general_cargo", "General cargo", 5),
    ("container", "Container", 6),
    ("passenger", "Passenger", 7),
    ("offshore_supply", "Offshore supply", 8),
    ("tug", "Tug", 9),
]


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register(c, email):
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
    return resp.json()


async def _verify_email(c, email):
    """31i: login gates on a verified email — mark it directly so the
    per-email SEND window stays pristine."""
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


def test_catalog_seeds_present_and_idempotent(arun):
    """Migration 0011 seeded the ten services and the nine vessel types
    (the registry's ship taxonomy, humanized labels, sort_order 1..n);
    the seed's ON CONFLICT shape means re-running the upsert changes
    nothing and duplicates nothing."""

    async def scenario():
        from sqlalchemy import text as sql

        from app.db import SessionLocal

        async with SessionLocal() as s:
            for table, key, expected in (
                ("service", "service_code", SERVICES),
                # 31r grew the vessel catalog to 15 rows (yachts/boats
                # appended); 31q's nine seeds stay the taxonomy PREFIX
                ("vessel_type", "ship_type", VESSEL_TYPES),
            ):
                rows = (
                    await s.execute(
                        sql(
                            f"SELECT {key}, label, sort_order FROM {table} "
                            "ORDER BY sort_order"
                        )
                    )
                ).all()
                assert [(r[0], r[1], r[2]) for r in rows[:len(expected)]] == \
                    expected, table

                # the migration's exact upsert shape, re-run: idempotent
                # (count unchanged, seeded labels canonical)
                before = len(rows)
                await s.execute(
                    sql(
                        f"INSERT INTO {table} ({key}, label, sort_order) "
                        "VALUES (:code, :label, :order) "
                        f"ON CONFLICT ({key}) DO UPDATE SET "
                        "label = EXCLUDED.label, sort_order = EXCLUDED.sort_order"
                    ),
                    [{"code": c, "label": l, "order": o} for c, l, o in expected],
                )
                again = (
                    await s.execute(
                        sql(f"SELECT COUNT(*) FROM {table}")
                    )
                ).scalar_one()
                assert again == before, table
                await s.rollback()  # leave the dev DB exactly as seeded

    arun(scenario())


def test_services_and_vessel_types_round_trip_replace_clear(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"ct-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.status_code == 200
            assert pub.json()["services"] == []
            assert pub.json()["vessel_types"] == []

            # save both fields (dedupe in the validator)
            r = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={
                    "services": ["draft_survey", "condition_survey",
                                 "draft_survey"],
                    "vessel_types": ["oil_tanker", "tug"],
                },
            )
            assert r.status_code == 200, r.text
            pub = await c.get(f"/api/orgs/{slug}")
            body = pub.json()
            # resolved to [{code, label}] rows in catalog sort order
            assert body["services"] == [
                {"code": "condition_survey", "label": "Condition survey"},
                {"code": "draft_survey", "label": "Draft survey"},
            ]
            assert body["vessel_types"] == [
                {"code": "oil_tanker", "label": "Oil tanker"},
                {"code": "tug", "label": "Tug"},
            ]

            # replace: the Save button submits the complete chip list
            r = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"services": ["bunker_quantity_survey"],
                      "vessel_types": ["gas_carrier"]},
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["services"] == [
                {"code": "bunker_quantity_survey",
                 "label": "Bunker quantity survey"},
            ]
            assert pub.json()["vessel_types"] == [
                {"code": "gas_carrier", "label": "Gas carrier"},
            ]

            # clear with empty lists; absent fields leave the rows alone
            r = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"services": [], "vessel_types": []},
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["services"] == []
            assert pub.json()["vessel_types"] == []
            r = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"about": "hi"}
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["services"] == []
            assert pub.json()["vessel_types"] == []

            # format guard: wrong-shape codes are rejected before any write
            for bad in (["Draft Survey"], ["UPPER"], ["x" * 65], ["bad-code"]):
                r = await c.put(
                    "/api/orgs/me/profile", headers=auth,
                    json={"services": bad},
                )
                assert r.status_code == 422, (bad, r.text)
            for bad in (["Oil Tanker"], ["OIL_TANKER"]):
                r = await c.put(
                    "/api/orgs/me/profile", headers=auth,
                    json={"vessel_types": bad},
                )
                assert r.status_code == 422, (bad, r.text)
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["services"] == []
            assert pub.json()["vessel_types"] == []

    arun(scenario())


def test_unknown_catalog_codes_rejected_atomically(arun):
    """Existence is identity's job for these catalogs: a code that is not
    in the catalog 422s (pydantic-shaped detail naming the field) and the
    org's previously saved selection is untouched — including the mixed
    known+unknown case (the whole save is atomic)."""

    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"cu-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            ok = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"services": ["draft_survey"]},
            )
            assert ok.status_code == 200

            bad = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"services": ["no_such_service"]},
            )
            assert bad.status_code == 422
            detail = bad.json()["detail"]
            assert detail[0]["loc"] == ["services"]
            assert "unknown service" in detail[0]["msg"]

            mixed = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"vessel_types": ["oil_tanker", "hovercraft"]},
            )
            assert mixed.status_code == 422
            assert mixed.json()["detail"][0]["loc"] == ["vessel_types"]
            assert "unknown vessel type" in mixed.json()["detail"][0]["msg"]

            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["services"] == [
                {"code": "draft_survey", "label": "Draft survey"}
            ]
            assert pub.json()["vessel_types"] == []

    arun(scenario())


def test_catalog_search_requires_only_a_session(arun):
    """Reference data — the wall only; any signed-in member queries it
    (catalog search needs no org permission)."""
    async def scenario():
        u = uuid.uuid4().hex[:8]
        async with _client() as c:
            anon = await c.get("/api/catalog/services/search?q=draft")
            assert anon.status_code == 401
            anon_vt = await c.get("/api/catalog/vessel-types/search?q=tank")
            assert anon_vt.status_code == 401

            email = f"cs-{u}@example.com"
            await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}

            by_label = await c.get(
                "/api/catalog/services/search?q=draft", headers=auth
            )
            assert by_label.status_code == 200
            assert {"code": "draft_survey", "label": "Draft survey"} in \
                by_label.json()["services"]
            # catalog sort order, not alphabetical
            all_svcs = (
                await c.get("/api/catalog/services/search?q=survey", headers=auth)
            ).json()["services"]
            assert [s["code"] for s in all_svcs] == [
                "pre_purchase_survey", "condition_survey",
                "bunker_quantity_survey", "on_hire_off_hire_survey",
                "draft_survey",
            ]

            by_code = await c.get(
                "/api/catalog/vessel-types/search?q=TANK", headers=auth
            )
            assert by_code.status_code == 200
            codes = [v["code"] for v in by_code.json()["vessel_types"]]
            assert "oil_tanker" in codes and "chemical_tanker" in codes
            # humanized labels
            vt = await c.get(
                "/api/catalog/vessel-types/search?q=offshore", headers=auth
            )
            assert vt.json()["vessel_types"] == [
                {"code": "offshore_supply", "label": "Offshore supply"}
            ]

    arun(scenario())


def test_services_require_profile_edit(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"ce-{u}@example.com"
            await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
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
                "/api/orgs/me/profile", headers=auth,
                json={"services": ["draft_survey"]},
            )
            assert denied.status_code == 403
            assert "org.profile.edit" in denied.json()["detail"]
            denied_vt = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"vessel_types": ["tug"]},
            )
            assert denied_vt.status_code == 403

    arun(scenario())
