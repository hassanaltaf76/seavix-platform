"""Registration + profile-management API tests (integration, local emulators).

Runs the FastAPI app in-process via httpx.ASGITransport inside ONE
asyncio.run per test — starlette TestClient breaks SQLAlchemy-asyncpg
connection reuse across requests (see reports/11-registration-api.md).
Requires: Firebase Auth emulator on localhost:9099 and the dev OpenFGA store.
"""
import asyncio
import io
import os
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


async def _register(c, slug, email, kind="survey_company"):
    resp = await c.post(
        "/api/register",
        json={
            "org_name": "Test Org",
            "org_type": "marine",
            "slug": slug,
            "email": email,
            "password": "secret-123",
            "account_kind": kind,
        },
    )
    return resp


async def _sign_in(email) -> str:
    async with httpx.AsyncClient(base_url="http://localhost:9099") as c:
        resp = await c.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
            params={"key": "demo-any-key"},
            json={"email": email, "password": "secret-123",
                  "returnSecureToken": True},
        )
    return resp.json()["idToken"]


def test_registration_happy_path_and_duplicate_slug(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"reg-{u}", f"reg-{u}@example.com"
            r1 = await _register(c, slug, email)
            assert r1.status_code == 201
            body = r1.json()
            assert body["slug"] == slug
            assert body["org_id"]

            r2 = await _register(c, slug, f"other-{u}@example.com")
            assert r2.status_code == 409
            assert r2.json()["detail"]["field"] == "slug"

            r3 = await _register(c, f"reg2-{u}", email)
            assert r3.status_code == 409
            assert r3.json()["detail"]["field"] == "email"

            g = await c.get(f"/api/orgs/{slug}")
            assert g.status_code == 200
            assert "email" not in g.text
            return slug, email

    arun(scenario())


def test_profile_update_logo_and_draft_to_live(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        received = []
        app.state.event_bus.subscribe("org.profile_published", received.append)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"prof-{u}", f"prof-{u}@example.com"
            await _register(c, slug, email)
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

            # draft -> live
            live = await c.post(
                "/api/orgs/me/profile-status",
                headers=auth,
                json={"status": "live"},
            )
            assert live.status_code == 200
            assert live.json()["profile_status"] == "live"
            g2 = await c.get(f"/api/orgs/{slug}")
            assert g2.json()["profile_status"] == "live"
            assert g2.json()["logo_url"].endswith(blob_key)
            assert received and received[-1]["type"] == "org.profile_published"

    arun(scenario())


def test_client_org_cannot_go_live_and_bad_status_400(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"cli-{u}", f"cli-{u}@example.com"
            await _register(c, slug, email, kind="client")
            await asyncio.sleep(0.2)
            token = await _sign_in(email)
            auth = {"Authorization": f"Bearer {token}"}

            r = await c.post("/api/orgs/me/profile-status", headers=auth,
                             json={"status": "live"})
            assert r.status_code == 400

            # survey company with invalid status value
            slug2, email2 = f"sc-{u}", f"sc-{u}@example.com"
            await _register(c, slug2, email2, kind="survey_company")
            token2 = await _sign_in(email2)
            r2 = await c.post("/api/orgs/me/profile-status",
                              headers={"Authorization": f"Bearer {token2}"},
                              json={"status": "suspended"})
            assert r2.status_code == 400

    arun(scenario())


def test_non_owner_gets_403(arun):
    """Deny path: registration writes an owner tuple; deleting it via the
    AuthzClient port leaves the membership row but no owner grant — the
    role check must then 403."""
    async def scenario():
        from seavix_ports import AuthzClient

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"own-{u}", f"own-{u}@example.com"
            r1 = await _register(c, slug, email)
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

            # revoke ownership, membership row remains
            await authz.delete_tuples(
                [{"user": f"user:{uid}", "relation": "owner",
                  "object": f"organization:{org_id}"}]
            )
            r = await c.put(
                "/api/orgs/me/profile",
                headers={"Authorization": f"Bearer {token}"},
                json={"about": "should not be allowed"},
            )
            assert r.status_code == 403

    arun(scenario())
