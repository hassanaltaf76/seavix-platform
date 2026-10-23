"""31r: vessel-type catalog extended with yachts and boats (three sizes).

Owner feedback (23 Oct): "vessel type does not include yachts and boats
small/medium/large". Migration 0012 (data-only) inserts six profile-only
rows into the identity vessel_type catalog, idempotent. The profile
catalog is now a SUPERSET of registry SHIP_TYPES — the registry's
vessel.ship_type CHECK is unchanged (operational taxonomy); yacht_*/
boat_* exist only in the marketing-facing profile catalog. These tests
pin the seeds, the idempotency, and the select round-trip through the
public read model.
"""
import sys
import uuid

import httpx

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.main import app  # noqa: E402

NEW_TYPES = [
    ("yacht_small", "Yacht — small", 10),
    ("yacht_medium", "Yacht — medium", 11),
    ("yacht_large", "Yacht — large", 12),
    ("boat_small", "Boat — small", 13),
    ("boat_medium", "Boat — medium", 14),
    ("boat_large", "Boat — large", 15),
]


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register(c, email):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Yacht Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _verify_email(c, email):
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


def test_yacht_boat_seeds_present_and_idempotent(arun):
    """0012 landed: the six rows exist with their em-dash labels and
    sort_order 10-15; the migration's ON CONFLICT DO NOTHING shape means
    a second run adds nothing (still 15 rows, labels untouched)."""

    async def scenario():
        from sqlalchemy import text as sql

        from app.db import SessionLocal

        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    sql(
                        "SELECT ship_type, label, sort_order "
                        "FROM vessel_type ORDER BY sort_order"
                    )
                )
            ).all()
            assert len(rows) == 15, rows
            assert [(r[0], r[1], r[2]) for r in rows[9:]] == NEW_TYPES
            # the original nine are untouched and stay the shared
            # vocabulary with the registry taxonomy
            assert [r[0] for r in rows[:9]] == [
                "oil_tanker", "chemical_tanker", "gas_carrier",
                "bulk_carrier", "general_cargo", "container", "passenger",
                "offshore_supply", "tug",
            ]

            # the migration's exact upsert, re-run: idempotent no-op
            await s.execute(
                sql(
                    "INSERT INTO vessel_type (ship_type, label, sort_order) "
                    "VALUES (:code, :label, :order) "
                    "ON CONFLICT (ship_type) DO NOTHING"
                ),
                [{"code": c, "label": l, "order": o} for c, l, o in NEW_TYPES],
            )
            count = (
                await s.execute(sql("SELECT COUNT(*) FROM vessel_type"))
            ).scalar_one()
            assert count == 15
            label = (
                await s.execute(
                    sql("SELECT label FROM vessel_type WHERE ship_type = 'yacht_large'")
                )
            ).scalar_one()
            assert label == "Yacht — large"
            await s.rollback()  # leave the dev DB exactly as seeded

    arun(scenario())


def test_yacht_boat_round_trip_through_profile(arun):
    """yacht_large + boat_small select, persist, and resolve through the
    public read model (catalog order: yacht_large 12 before boat_small
    13); unknown-shape codes are still rejected."""

    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"yb-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            r = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={"vessel_types": ["boat_small", "yacht_large"]},
            )
            assert r.status_code == 200, r.text
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["vessel_types"] == [
                {"code": "yacht_large", "label": "Yacht — large"},
                {"code": "boat_small", "label": "Boat — small"},
            ]

            # replaced by a clear, like every other field
            r = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"vessel_types": []},
            )
            assert r.status_code == 200
            pub = await c.get(f"/api/orgs/{slug}")
            assert pub.json()["vessel_types"] == []

            # registry-only shapes still rejected (hovercraft is neither
            # in the registry taxonomy nor the profile catalog)
            bad = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"vessel_types": ["hovercraft"]},
            )
            assert bad.status_code == 422
            assert "unknown vessel type" in bad.json()["detail"][0]["msg"]

    arun(scenario())
