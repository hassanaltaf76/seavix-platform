"""OIDC relying-party plumbing tests (D3): fake id_token payloads.

The emulator cannot exchange external-IdP tokens (documented limitation),
so the RP flow is unit-tested end-to-end with synthetic unsigned JWTs:
FakeOIDCProvider checks structure/iss/aud/exp only — signature verification
is Identity Platform's job in prod (docs/AUTH-ARCHITECTURE.md §b).
"""
import base64
import json
import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from seavix_ports.oidc import (  # noqa: E402
    ClaimsMapper,
    FakeOIDCProvider,
    OidcProviderConfig,
    OidcSignInError,
    decode_oidc_token,
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_id_token(claims: dict) -> str:
    return ".".join(
        [
            _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode()),
            _b64(json.dumps(claims).encode()),
            _b64(b"fake-signature"),
        ]
    )


ISSUER = "https://idp.example.test"
CLIENT_ID = "seavix-rp"


def _config(mapping: dict | None = None) -> OidcProviderConfig:
    return OidcProviderConfig(
        display_name="test-idp",
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret_ref="OIDC_TEST_SECRET",
        claims_mapping=mapping or {},
    )


def test_decode_and_map_claims():
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "email": "ada@example.test",
        "name": "Ada Lovelace",
        "groups": ["grp-ops-uuid", "grp-nope", "grp-admin-uuid"],
    }
    token = fake_id_token(claims)
    assert decode_oidc_token(token)["email"] == "ada@example.test"

    mapping = {
        "email_claim": "email",
        "name_claim": "name",
        "role_claim": "groups",
        "roles": {
            "grp-ops-uuid": "ops",
            "grp-admin-uuid": "admin",
        },
    }
    mapped = ClaimsMapper().map(decode_oidc_token(token), mapping)
    assert mapped["email"] == "ada@example.test"
    assert mapped["name"] == "Ada Lovelace"
    assert mapped["roles"] == ["ops", "admin"]  # unmapped group dropped

    # defaults: email/name claims, no role claim
    minimal = ClaimsMapper().map(
        {"email": "x@y.z", "name": "X"}, {}
    )
    assert minimal == {"email": "x@y.z", "name": "X", "roles": []}


def test_fake_provider_checks_iss_aud(arun):
    async def scenario():
        provider = FakeOIDCProvider(_config())
        good = fake_id_token(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "s1",
             "email": "a@b.c"}
        )
        user = await provider.sign_in(good)
        assert user.uid == f"oidc:{ISSUER}:s1"
        assert user.email == "a@b.c"

        with pytest.raises(OidcSignInError):
            await provider.sign_in(
                fake_id_token({"iss": "https://evil", "aud": CLIENT_ID,
                               "sub": "s", "email": "a@b.c"})
            )
        with pytest.raises(OidcSignInError):
            await provider.sign_in(
                fake_id_token({"iss": ISSUER, "aud": "other-client",
                               "sub": "s", "email": "a@b.c"})
            )
        with pytest.raises(OidcSignInError):
            await provider.sign_in(
                fake_id_token({"iss": ISSUER, "aud": CLIENT_ID, "sub": "s"})
            )

    arun(scenario())


# ------------------------------------------------- callback (API level)

def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _auth(sid):
    return {"X-Seavix-Session": sid}


async def _register_and_login(c, slug, email):
    r = await c.post(
        "/api/register",
        json={
            "org_name": "OIDC Org", "org_type": "marine", "slug": slug,
            "email": email, "password": "secret-123",
            "account_kind": "survey_company",
        },
    )
    assert r.status_code == 201, r.text
    start = await c.post(
        "/api/auth/login/start",
        json={"email": email, "password": "secret-123"},
    )
    code = (
        await c.get("/api/dev/last-otp", params={"email": email})
    ).json()["code"]
    done = await c.post(
        "/api/auth/login/verify", json={"email": email, "code": code}
    )
    return r.json()["org_id"], done.json()["session_id"]


MAPPING = {
    "email_claim": "email",
    "name_claim": "name",
    "role_claim": "groups",
    "roles": {"grp-ops": "ops", "grp-viewer": "viewer", "grp-ghost": "ghost-role"},
}


def test_oidc_callback_jit_provisioning(arun):
    async def scenario():
        async with _client() as c:
            u = uuid.uuid4().hex[:8]
            slug, email = f"oid-{u}", f"oid-{u}@example.com"
            org_id, sid = await _register_and_login(c, slug, email)
            auth = _auth(sid)

            made = await c.post(
                "/api/orgs/me/idps", headers=auth,
                json={
                    "display_name": "test-idp",
                    "issuer": ISSUER,
                    "client_id": CLIENT_ID,
                    "client_secret_ref": "OIDC_TEST_SECRET",
                    "claims_mapping": MAPPING,
                },
            )
            assert made.status_code == 201, made.text
            listed = await c.get("/api/orgs/me/idps", headers=auth)
            assert listed.status_code == 200
            assert listed.json()["identity_providers"][0]["display_name"] == "test-idp"

            external_email = f"ext-{u}@example.test"
            token = fake_id_token(
                {
                    "iss": ISSUER,
                    "aud": CLIENT_ID,
                    "sub": f"sub-{u}",
                    "email": external_email,
                    "name": "External User",
                    "groups": ["grp-ops", "grp-ghost"],
                }
            )
            cb = await c.post(
                "/api/public/auth/oidc/callback",
                json={"org_slug": slug, "idp_name": "test-idp", "id_token": token},
            )
            assert cb.status_code == 201, cb.text
            body = cb.json()
            assert body["email"] == external_email
            assert body["roles"] == ["ops", "ghost-role"]  # claim-mapped names
            assert body["roles_skipped"] == ["ghost-role"]  # missing in org
            assert body["memberships"][0]["slug"] == slug

            # session works and the AuthContext carries the mapped role
            me = await c.get(
                "/api/internal/auth-context",
                headers={"X-Seavix-Session": body["session_id"]},
            )
            assert me.status_code == 200
            membership = me.json()["memberships"][0]
            assert membership["roles"] == ["ops"]
            assert "quote.create" in membership["permissions"]

            # second sign-in: same user, no duplicate rows
            token2 = fake_id_token(
                {"iss": ISSUER, "aud": CLIENT_ID, "sub": f"sub-{u}",
                 "email": external_email, "name": "External User",
                 "groups": ["grp-ops"]}
            )
            cb2 = await c.post(
                "/api/public/auth/oidc/callback",
                json={"org_slug": slug, "idp_name": "test-idp", "id_token": token2},
            )
            assert cb2.status_code == 201
            from sqlalchemy import text as sql

            from app.db import SessionLocal

            async with SessionLocal() as s:
                n = (
                    await s.execute(
                        sql("SELECT count(*) FROM app_user WHERE email = :e"),
                        {"e": external_email},
                    )
                ).scalar_one()
                assert n == 1

            # wrong issuer -> 401; unknown org/idp -> 404
            bad = await c.post(
                "/api/public/auth/oidc/callback",
                json={
                    "org_slug": slug, "idp_name": "test-idp",
                    "id_token": fake_id_token(
                        {"iss": "https://evil", "aud": CLIENT_ID, "sub": "s",
                         "email": external_email}
                    ),
                },
            )
            assert bad.status_code == 401
            noidp = await c.post(
                "/api/public/auth/oidc/callback",
                json={
                    "org_slug": slug, "idp_name": "ghost",
                    "id_token": token2,
                },
            )
            assert noidp.status_code == 404

            # linking onto an existing password account is refused
            clash = await c.post(
                "/api/public/auth/oidc/callback",
                json={
                    "org_slug": slug, "idp_name": "test-idp",
                    "id_token": fake_id_token(
                        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "s-owner",
                         "email": email, "groups": []}
                    ),
                },
            )
            assert clash.status_code == 409

            # registry CRUD guards: viewer cannot manage IdPs
            roles = (
                await c.get("/api/orgs/me/roles", headers=auth)
            ).json()["roles"]
            viewer = next(r for r in roles if r["name"] == "viewer")
            me_id = (
                await c.get("/api/auth/session", headers=auth)
            ).json()["user_id"]
            await c.put(
                f"/api/orgs/me/members/{me_id}/roles", headers=auth,
                json={"role_ids": [viewer["role_id"]]},
            )
            denied = await c.get("/api/orgs/me/idps", headers=auth)
            assert denied.status_code == 403

    arun(scenario())
