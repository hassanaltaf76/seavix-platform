"""31m: public contact details — persistence and validation behind
PUT /api/orgs/me/profile and the public read model.

The contact block (address / phone / contact_email / whatsapp) is public
profile content: contact_email is the PUBLIC business contact and is
never derived from the account email — the read model must never carry
the account address.
"""
import json
import sys
import uuid

import httpx

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _register(c, email):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Contact Org",
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(c, email):
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()
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
    assert done.status_code == 200
    return done.json()["session_id"]


def test_contact_block_round_trip_on_public_read_model(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"ct-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            CONTACT_FIELDS = (
                "address", "address_line1", "address_line2", "city", "state",
                "postal_code", "country_code", "phone_cc", "phone",
                "contact_email", "whatsapp_cc", "whatsapp",
            )
            pub = await c.get(f"/api/orgs/{slug}")
            for field in CONTACT_FIELDS:
                assert pub.json()[field] is None

            payload = {
                "about": "Independent marine surveyors.",
                # legacy free text still accepts/stores (deprecated)
                "address": "Suite 1404, Marina Plaza\nDubai Marina, Dubai, UAE",
                "address_line1": "Suite 1404, Marina Plaza",
                "address_line2": "Marina Tower",
                "city": "Dubai",
                "state": "Dubai",
                "postal_code": "00000",
                "country_code": "AE",
                "phone_cc": "+971",
                "phone": "4 555 0100",
                "contact_email": f"ops-{u}@harbor.example",
                "whatsapp_cc": "+44",
                "whatsapp": "7700 900123",
            }
            r = await c.put("/api/orgs/me/profile", headers=auth, json=payload)
            assert r.status_code == 200, r.text

            pub = await c.get(f"/api/orgs/{slug}")
            body = pub.json()
            for field, value in payload.items():
                assert body[field] == value, field
            # surface separation: the account email NEVER enters the
            # public read model, even as a substring
            assert email not in json.dumps(body)

            # clear every contact field (empty string behaves as null)
            r = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={**{k: "" for k in CONTACT_FIELDS},
                      "about": payload["about"]},
            )
            assert r.status_code == 200, r.text
            pub = await c.get(f"/api/orgs/{slug}")
            for field in CONTACT_FIELDS:
                assert pub.json()[field] is None

    arun(scenario())


def test_contact_validation_rejects_bad_values_atomically(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"cv-{u}@example.com"
            reg = await _register(c, email)
            sid = await _login(c, email)
            auth = {"X-Seavix-Session": sid}
            slug = reg["slug"]

            good = {
                "address": "Pier 7, JBR Walk",
                "country_code": "AE",
                "phone_cc": "+971",
                "phone": "4 555 0100",
                "contact_email": f"ops-{u}@harbor.example",
                "whatsapp_cc": "+971",
                "whatsapp": "50 123 4567",
            }
            r = await c.put("/api/orgs/me/profile", headers=auth, json=good)
            assert r.status_code == 200, r.text

            # 31t national-number rule (phone AND whatsapp): digits,
            # spaces, dashes only; 5-14 digits; the country code has its
            # own column — each rejected before any write
            for bad_field, bad_value in (
                ("contact_email", "not-an-email"),
                ("whatsapp", "call-me-now"),
                ("phone", "call-me-now"),
                ("whatsapp", "1 2"),            # too few digits
                ("phone", "1" * 15),            # too many digits
                ("whatsapp", "+971 50 123 4567"),  # cc embedded
                ("phone", "+44 (0)20 7946"),       # parens superseded
                ("phone_cc", "971"),               # missing +
                ("phone_cc", "+12345"),            # too long
                ("whatsapp_cc", "abc"),
                ("country_code", "ae"),            # not alpha-2
                ("country_code", "UAE"),
            ):
                r = await c.put(
                    "/api/orgs/me/profile",
                    headers=auth,
                    json={**good, bad_field: bad_value},
                )
                assert r.status_code == 422, (bad_field, bad_value, r.text)
                assert bad_field in r.text  # loc pins the offending field
                # atomic: the stored block is untouched
                pub = await c.get(f"/api/orgs/{slug}")
                for field, value in good.items():
                    assert pub.json()[field] == value, (bad_field, field)

            # legal national formats ARE accepted (spaces/dashes, 5-14)
            for national in ("501234567", "50 123 4567", "50-123-4567",
                             "5 0123-4567"):
                r = await c.put(
                    "/api/orgs/me/profile",
                    headers=auth,
                    json={**good, "whatsapp": national},
                )
                assert r.status_code == 200, (national, r.text)
                pub = await c.get(f"/api/orgs/{slug}")
                assert pub.json()["whatsapp"] == national

    arun(scenario())
