"""32b (identity side): client contact person + designation, the
ship-operator funnel registration (org_type='client' binds the 'client'
system role — the registered-client rail shows RFQs only), and the
verification link's `next` passthrough for the public RFQ funnel.
"""
import os
import re
import sys
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


def _client(**transport_kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **transport_kwargs),
        base_url="http://t",
    )


def test_client_org_contact_person_and_designation(arun):
    """32a amendment (32b): a quote addresses a person — "Dear Capt. Amin
    Dar, Managing Director" (Pak Marine quote-doc field spec, §4)."""
    async def scenario():
        async with _client() as c:
            email = f"cp-{uuid.uuid4().hex[:8]}@example.com"
            r = await c.post("/api/internal/orgs", json={
                "type": "client", "name": "Dar Shipping", "email": email,
                "contact_person": "Capt. Amin Dar",
                "designation": "Managing Director",
            })
            assert r.status_code == 201, r.text
            org_id = r.json()["org_id"]
            assert r.json()["contact_person"] == "Capt. Amin Dar"
            assert r.json()["designation"] == "Managing Director"

            g = await c.get(f"/api/internal/orgs/id/{org_id}")
            assert g.status_code == 200
            assert g.json()["contact_person"] == "Capt. Amin Dar"
            assert g.json()["designation"] == "Managing Director"

            # optional + blank-to-none
            r2 = await c.post("/api/internal/orgs", json={
                "type": "client", "name": "Bare Client",
                "email": f"cp2-{uuid.uuid4().hex[:8]}@example.com",
                "contact_person": "   ",
            })
            assert r2.status_code == 201, r2.text
            g2 = await c.get(
                f"/api/internal/orgs/id/{r2.json()['org_id']}")
            assert g2.json()["contact_person"] is None
            assert g2.json()["designation"] is None

    arun(scenario())


def test_client_org_contact_person_validation(arun):
    async def scenario():
        async with _client() as c:
            too_long = "x" * 121
            r = await c.post("/api/internal/orgs", json={
                "type": "client", "name": "Long Person",
                "email": f"cp3-{uuid.uuid4().hex[:8]}@example.com",
                "contact_person": too_long,
            })
            assert r.status_code == 422
            r = await c.post("/api/internal/orgs", json={
                "type": "client", "name": "Long Designation",
                "email": f"cp4-{uuid.uuid4().hex[:8]}@example.com",
                "designation": too_long,
            })
            assert r.status_code == 422

    arun(scenario())


def test_client_type_registration_binds_client_role(arun):
    """The funnel registration: org_type='client' creates the org, and the
    first member is issued the 'client' system role — rfq.create +
    rfq.view ONLY (the registered-client rail shows RFQs and nothing
    else). Other org types keep the owner binding."""
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            email = f"funnel-{u}@example.com"
            reg = await c.post("/api/register", json={
                "org_name": f"Dar Chartering {u}",
                "org_type": "client",
                "email": email,
                "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg.status_code == 201, reg.text
            org_id = reg.json()["org_id"]

            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                row = (await s.execute(
                    sql("SELECT type FROM organization WHERE id = :o"),
                    {"o": org_id})).one()
                assert row[0] == "client"
                role = (await s.execute(
                    sql("SELECT role FROM membership WHERE org_id = :o"),
                    {"o": org_id})).scalar_one()
                assert role == "client"

            # the auth context the web shell consumes: ONLY rfq permissions
            # (31i: login gates on a verified email — mark it in the DB,
            # the same seam the other identity tests use)
            async with SessionLocal() as s:
                await s.execute(
                    sql("UPDATE app_user SET email_verified = true "
                        "WHERE email = :e"),
                    {"e": email},
                )
                await s.commit()
            start = await c.post("/api/auth/login/start",
                                 json={"email": email, "password": "secret-123"})
            assert start.status_code == 200, start.text
            code = (await c.get("/api/dev/last-otp",
                                params={"email": email})).json()["code"]
            done = await c.post("/api/auth/login/verify",
                                json={"email": email, "code": code})
            assert done.status_code == 200, done.text
            sid = done.json()["session_id"]
            ctx = await c.get("/api/internal/auth-context",
                              headers={"X-Seavix-Session": sid})
            assert ctx.status_code == 200
            memberships = ctx.json()["memberships"]
            assert len(memberships) == 1
            m = memberships[0]
            assert m["org_type"] == "client"
            assert sorted(m["permissions"]) == ["rfq.create", "rfq.view"]

            # survey-company registration still binds owner
            owner_email = f"owner-{u}@example.com"
            reg2 = await c.post("/api/register", json={
                "org_name": f"Survey Co {u}",
                "org_type": "survey_company",
                "email": owner_email,
                "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg2.status_code == 201
            from sqlalchemy import text as sql2

            async with SessionLocal() as s:
                role2 = (await s.execute(
                    sql2("SELECT role FROM membership WHERE org_id = :o"),
                    {"o": reg2.json()["org_id"]})).scalar_one()
                assert role2 == "owner"

    arun(scenario())


def test_verify_send_next_rides_the_link(arun):
    """32b funnel: the verification email's confirm link carries `next`
    so post-login can return the new user to the RFQ form. Local paths
    only."""
    async def scenario():
        async with _client() as c:
            email = f"next-{uuid.uuid4().hex[:8]}@example.com"
            reg = await c.post("/api/register", json={
                "org_name": "Next Co", "org_type": "client", "email": email,
                "password": "secret-123", "confirm_password": "secret-123",
            })
            assert reg.status_code == 201
            sent = await c.post("/api/register/verify/send", json={
                "email": email, "next": "/org/pak-marine/rfq",
            })
            assert sent.status_code == 200, sent.text

            from app.mailer import get_mailer

            mail = next(m for m in reversed(get_mailer().outbox)
                        if m.to == email)
            line = next(l for l in mail.body_text.splitlines()
                        if "oob_code=" in l)
            qs = parse_qs(urlparse(line.strip()).query)
            assert qs["next"] == ["/org/pak-marine/rfq"]
            assert "/register/verify/confirm" in line

            # open redirects rejected; absence stays absent
            bad = await c.post("/api/register/verify/send", json={
                "email": email, "next": "https://evil.example/x",
            })
            assert bad.status_code == 422
            bad2 = await c.post("/api/register/verify/send", json={
                "email": email, "next": "//evil.example",
            })
            assert bad2.status_code == 422

    arun(scenario())
