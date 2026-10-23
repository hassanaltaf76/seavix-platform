"""Registration + profile-management API tests (integration, local emulators).

Runs the FastAPI app in-process via httpx.ASGITransport inside ONE
asyncio.run per test — starlette TestClient breaks SQLAlchemy-asyncpg
connection reuse across requests (see reports/11-registration-api.md).
Requires: Firebase Auth emulator on localhost:9099 and the dev OpenFGA store.
"""
import asyncio
import io
import os
import re
import uuid

os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
os.environ.setdefault("FIREBASE_PROJECT_ID", "seavix-dev")
os.environ.setdefault("OPENFGA_HTTP_ADDR", "localhost:18080")
os.environ.setdefault("OPENFGA_STORE_ID", "01M45WT1F2VSVX6SKWGRTY17BB")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402


def _png_bytes(size=(8, 8), color=(20, 90, 140)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _register(c, slug=None, email=None, org_type="survey_company",
                    password="secret-123", confirm=None):
    payload = {
        "org_name": "Test Org",
        "org_type": org_type,
        "email": email,
        "password": password,
        "confirm_password": confirm if confirm is not None else password,
    }
    return await c.post("/api/register", json=payload)


async def _sign_in(email) -> str:
    async with httpx.AsyncClient(base_url="http://localhost:9099") as c:
        resp = await c.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
            params={"key": "demo-any-key"},
            json={"email": email, "password": "secret-123",
                  "returnSecureToken": True},
        )
    return resp.json()["idToken"]


def test_registration_happy_path_server_slug_and_dup_email(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            email = f"reg-{u}@example.com"
            r1 = await _register(c, email=email)
            assert r1.status_code == 201
            body = r1.json()
            assert body["org_id"]
            # slug is server-generated (placeholder), format-valid, unique
            slug = body["slug"]
            assert re.fullmatch(r"[a-z0-9-]+", slug)
            g = await c.get(f"/api/orgs/{slug}")
            assert g.status_code == 200
            assert g.json()["type"] == "survey_company"
            # public-safe JSON: never the account email VALUE (31m adds the
            # contact_email KEY — the public business contact, null until set)
            assert email not in g.text
            assert g.json()["contact_email"] is None

            # duplicate email -> field-level 409
            r3 = await _register(c, email=email)
            assert r3.status_code == 409
            assert r3.json()["detail"]["field"] == "email"
            return slug, email

    arun(scenario())


def test_profile_update_logo_and_draft_to_live(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        received = []
        app.state.event_bus.subscribe("org.profile_published", received.append)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            email = f"prof-{u}@example.com"
            reg = await _register(c, email=email)
            slug = reg.json()["slug"]
            chosen = f"harbor-{u}"
            await asyncio.sleep(0.2)  # let the owner-tuple task finish
            token = await _sign_in(email)
            auth = {"Authorization": f"Bearer {token}"}

            # draft initially
            g = await c.get(f"/api/orgs/{slug}")
            assert g.json()["profile_status"] == "draft"

            # update profile fields
            p = await c.put(
                "/api/orgs/me/profile",
                headers=auth,
                json={"about": "Surveyors.", "website": "https://x.example",
                      "phone": "+1 555 0100"},
            )
            assert p.status_code == 200

            # set the public slug (profile settings); about/website/phone keep
            s = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"slug": chosen}
            )
            assert s.status_code == 200
            assert (await c.get(f"/api/orgs/{chosen}")).status_code == 200
            assert (await c.get(f"/api/orgs/{slug}")).status_code == 404

            # slug conflicts and bad formats are clean field-level errors
            other = await _register(c, email=f"other-{u}@example.com")
            taken = await c.put(
                "/api/orgs/me/profile", headers=auth,
                json={"slug": other.json()["slug"]},
            )
            assert taken.status_code == 409
            assert taken.json()["detail"]["field"] == "slug"
            bad = await c.put(
                "/api/orgs/me/profile", headers=auth, json={"slug": "Bad Slug!"}
            )
            assert bad.status_code == 422

            # app paths (current + planned) are reserved at the root URL
            for reserved in ("login", "search", "dashboard"):
                r = await c.put(
                    "/api/orgs/me/profile", headers=auth, json={"slug": reserved}
                )
                assert r.status_code == 422, reserved
                assert "reserved" in str(r.json()["detail"])

            # small PNG accepted
            logo = await c.post(
                "/api/orgs/me/logo",
                headers=auth,
                files={"file": ("logo.png", _png_bytes(), "image/png")},
            )
            assert logo.status_code == 200
            blob_key = logo.json()["logo_blob_key"]
            assert blob_key.startswith("logos/")

            # oversize rejected (valid PNG signature, >2MB)
            fat = _png_bytes() + b"\x00" * (2 * 1024 * 1024)
            big = await c.post(
                "/api/orgs/me/logo",
                headers=auth,
                files={"file": ("big.png", fat, "image/png")},
            )
            assert big.status_code == 413

            # non-image rejected
            txt = await c.post(
                "/api/orgs/me/logo",
                headers=auth,
                files={"file": ("notes.txt", b"hello world", "text/plain")},
            )
            assert txt.status_code == 415

            # draft -> live requires a verified email (registration gate)
            live = await c.post(
                "/api/orgs/me/profile-status",
                headers=auth,
                json={"status": "live"},
            )
            assert live.status_code == 403

            await c.post("/api/register/verify/send", json={"email": email})
            from urllib.parse import parse_qs, urlparse

            from app.mailer import get_mailer

            mail = next(
                m for m in reversed(get_mailer().outbox)
                if m.to == email and m.subject.startswith("Verify your SeaVix")
            )
            line = next(l for l in mail.body_text.splitlines() if "oob_code=" in l)
            oob = parse_qs(urlparse(line.strip()).query)["oob_code"][0]
            ok = await c.post(
                "/api/register/verify/confirm",
                json={"email": email, "oob_code": oob},
            )
            assert ok.status_code == 200

            live = await c.post(
                "/api/orgs/me/profile-status",
                headers=auth,
                json={"status": "live"},
            )
            assert live.status_code == 200
            assert live.json()["profile_status"] == "live"
            g2 = await c.get(f"/api/orgs/{chosen}")
            assert g2.json()["profile_status"] == "live"
            assert g2.json()["logo_url"].endswith(blob_key)
            assert received and received[-1]["type"] == "org.profile_published"

    arun(scenario())


def test_non_survey_org_cannot_go_live_and_bad_status_400(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            email = f"cli-{u}@example.com"
            await _register(c, email=email, org_type="management")
            await asyncio.sleep(0.2)
            token = await _sign_in(email)
            auth = {"Authorization": f"Bearer {token}"}

            r = await c.post("/api/orgs/me/profile-status", headers=auth,
                             json={"status": "live"})
            assert r.status_code == 400

            # survey company with invalid status value
            email2 = f"sc-{u}@example.com"
            await _register(c, email=email2, org_type="survey_company")
            token2 = await _sign_in(email2)
            r2 = await c.post("/api/orgs/me/profile-status",
                              headers={"Authorization": f"Bearer {token2}"},
                              json={"status": "suspended"})
            assert r2.status_code == 400

    arun(scenario())


def test_member_without_permission_gets_403(arun):
    """Deny path: registration still writes the OpenFGA owner tuple (cross-org
    grant layer), but intra-org route access is now RBAC — stripping the
    membership's role assignments leaves the membership row but no
    permissions, so profile edit must 403."""
    async def scenario():
        from seavix_ports import AuthzClient
        from sqlalchemy import text as sql

        from app.db import SessionLocal

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            email = f"own-{u}@example.com"
            r1 = await _register(c, email=email)
            org_id = r1.json()["org_id"]
            await asyncio.sleep(0.2)
            token = await _sign_in(email)

            async with httpx.AsyncClient(base_url="http://localhost:9099") as fc:
                info = await fc.post(
                    "/identitytoolkit.googleapis.com/v1/accounts:lookup",
                    params={"key": "demo-any-key"},
                    json={"idToken": token},
                )
            uid = info.json()["users"][0]["localId"]

            authz = AuthzClient.from_env()
            assert await authz.check(f"user:{uid}", "owner",
                                     f"organization:{org_id}") is True

            # strip ALL role assignments (membership row remains, now with
            # an empty permission union)
            async with SessionLocal() as s:
                app_user = (
                    await s.execute(
                        sql("SELECT id FROM app_user WHERE firebase_uid = :uid"),
                        {"uid": uid},
                    )
                ).scalar_one()
                await s.execute(
                    sql(
                        "DELETE FROM membership_role WHERE user_id = :uid AND org_id = :org"
                    ),
                    {"uid": str(app_user), "org": org_id},
                )
                await s.commit()

            r = await c.put(
                "/api/orgs/me/profile",
                headers={"Authorization": f"Bearer {token}"},
                json={"about": "should not be allowed"},
            )
            assert r.status_code == 403
            assert "org.profile.edit" in r.json()["detail"]

    arun(scenario())
