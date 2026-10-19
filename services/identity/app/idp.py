"""Identity-provider registry + OIDC relying-party callback (D3).

Org-scoped registry rows (identity_provider, migration 0006) describe each
external OIDC IdP. SeaVix stays the relying party THROUGH Identity
Platform: in prod the external token is exchanged for a SeaVix token and
the Admin SDK path is unchanged. Until the GCP project exists (D3), the
callback uses the FakeOIDCProvider against synthetic tokens — the full
JIT-provisioning plumbing (claims -> roles, membership creation, session
issuance) is exercised for real, only the signature check is structural.

Callback contract (dev): POST /api/public/auth/oidc/callback
  {org_slug, idp_name, id_token} -> {session_id, email, memberships}
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from seavix_ports import AuthContext, require_auth
from seavix_ports.oidc import (
    ClaimsMapper,
    FakeOIDCProvider,
    OidcProviderConfig,
    OidcSignInError,
)
from sqlalchemy import JSON, bindparam, text as sql

from seavix_ports import domain_event, public_route

from . import sessions
from .db import SessionLocal
from .permissions import Permission
from .rbac import assign_roles
from .security import require_permission

router = APIRouter()


def _my_membership(ctx: AuthContext):
    membership = ctx.memberships[0] if ctx.memberships else None
    if membership is None:
        raise HTTPException(status_code=403, detail="no organization membership")
    return membership


class IdpCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    issuer: str = Field(min_length=1, max_length=300)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret_ref: str = Field(min_length=1, max_length=300)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    claims_mapping: dict = Field(default_factory=dict)


def _row_to_config(row: dict) -> OidcProviderConfig:
    return OidcProviderConfig(
        display_name=row["display_name"],
        issuer=row["issuer"],
        client_id=row["client_id"],
        client_secret_ref=row["client_secret_ref"],
        scopes=tuple(row["scopes"]),
        claims_mapping=dict(row["claims_mapping"]),
        enabled=bool(row["enabled"]),
    )


# ------------------------------------------------------------- registry CRUD

@router.get("/api/orgs/me/idps")
async def list_idps(request: Request, ctx: AuthContext = Depends(require_auth)):
    membership = require_permission(ctx, Permission.ROLE_MANAGE.value)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                sql(
                    "SELECT id, display_name, issuer, client_id, scopes, "
                    "claims_mapping, enabled, created_at FROM identity_provider "
                    "WHERE org_id = :org ORDER BY created_at"
                ),
                {"org": membership.org_id},
            )
        ).mappings().all()
    return {
        "org_id": membership.org_id,
        "identity_providers": [
            {
                "idp_id": str(r["id"]),
                "display_name": r["display_name"],
                "issuer": r["issuer"],
                "client_id": r["client_id"],
                "scopes": list(r["scopes"]),
                "claims_mapping": dict(r["claims_mapping"]),
                "enabled": bool(r["enabled"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.post("/api/orgs/me/idps", status_code=201)
async def create_idp(
    payload: IdpCreate, request: Request,
    ctx: AuthContext = Depends(require_auth),
):
    membership = require_permission(ctx, Permission.ROLE_MANAGE.value)
    async with SessionLocal() as session:
        clash = (
            await session.execute(
                sql(
                    "SELECT 1 FROM identity_provider WHERE org_id = :org "
                    "AND display_name = :name"
                ),
                {"org": membership.org_id, "name": payload.display_name},
            )
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="identity provider name exists")
        idp_id = str(uuid.uuid4())
        insert = sql(
            "INSERT INTO identity_provider (id, org_id, display_name, issuer, "
            "client_id, client_secret_ref, scopes, claims_mapping, enabled) "
            "VALUES (:id, :org, :name, :issuer, :cid, :cref, :scopes, :mapping, true)"
        ).bindparams(
            bindparam("scopes", type_=JSON), bindparam("mapping", type_=JSON)
        )
        await session.execute(
            insert,
            {
                "id": idp_id,
                "org": membership.org_id,
                "name": payload.display_name,
                "issuer": payload.issuer,
                "cid": payload.client_id,
                "cref": payload.client_secret_ref,
                "scopes": payload.scopes,
                "mapping": payload.claims_mapping,
            },
        )
        await session.commit()
    return {"idp_id": idp_id, "display_name": payload.display_name, "enabled": True}


@router.delete("/api/orgs/me/idps/{idp_id}")
async def delete_idp(
    idp_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    membership = require_permission(ctx, Permission.ROLE_MANAGE.value)
    async with SessionLocal() as session:
        result = await session.execute(
            sql("DELETE FROM identity_provider WHERE id = :id AND org_id = :org"),
            {"id": idp_id, "org": membership.org_id},
        )
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="identity provider not found")
    return {"deleted": idp_id}


# ------------------------------------------------------------- RP callback

class OidcCallback(BaseModel):
    org_slug: str = Field(min_length=1)
    idp_name: str = Field(min_length=1)
    id_token: str = Field(min_length=1)


@router.post("/api/public/auth/oidc/callback", status_code=201)
@public_route
async def oidc_callback(payload: OidcCallback, request: Request) -> dict:
    """External IdP sign-in (D3). Dev: FakeOIDCProvider verifies structure
    only. JIT-provisions the user and binds the mapped roles, then issues
    the usual opaque session."""
    async with SessionLocal() as session:
        org = (
            await session.execute(
                sql("SELECT id FROM organization WHERE slug = :slug"),
                {"slug": payload.org_slug},
            )
        ).first()
        if org is None:
            raise HTTPException(status_code=404, detail="unknown organization")
        row = (
            await session.execute(
                sql(
                    "SELECT * FROM identity_provider WHERE org_id = :org "
                    "AND display_name = :name AND enabled"
                ),
                {"org": str(org[0]), "name": payload.idp_name},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="identity provider not found")
        config = _row_to_config(dict(row))

    # Dev/test RP stand-in (D3). Prod swaps in IdentityPlatformOIDCProvider.
    provider = FakeOIDCProvider(config)
    try:
        external = await provider.sign_in(payload.id_token)
    except (OidcSignInError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=f"invalid id token: {exc}") from None

    from seavix_ports.oidc import decode_oidc_token

    claims = decode_oidc_token(payload.id_token)
    mapped = ClaimsMapper().map(claims, config.claims_mapping)

    async with SessionLocal() as session:
        user = (
            await session.execute(
                sql("SELECT id, firebase_uid FROM app_user WHERE email = :email"),
                {"email": external.email},
            )
        ).mappings().first()
        if user is not None and not str(user["firebase_uid"]).startswith("oidc:"):
            # Linking IdP identities onto existing password accounts is a
            # prod decision (verified-email linking) — not silently allowed.
            raise HTTPException(
                status_code=409,
                detail="an account with this email already exists — linking is a prod step",
            )
        if user is None:
            user_id = str(uuid.uuid4())
            await session.execute(
                sql(
                    "INSERT INTO app_user (id, email, firebase_uid) "
                    "VALUES (:id, :email, :fb)"
                ),
                {"id": user_id, "email": external.email, "fb": external.uid},
            )
            await session.execute(
                sql(
                    "INSERT INTO membership (user_id, org_id, role, status) "
                    "VALUES (:uid, :org, 'member', 'active')"
                ),
                {"uid": user_id, "org": str(org[0])},
            )
        else:
            user_id = str(user["id"])
            member = (
                await session.execute(
                    sql(
                        "SELECT 1 FROM membership WHERE user_id = :uid AND org_id = :org"
                    ),
                    {"uid": user_id, "org": str(org[0])},
                )
            ).first()
            if member is None:
                await session.execute(
                    sql(
                        "INSERT INTO membership (user_id, org_id, role, status) "
                        "VALUES (:uid, :org, 'member', 'active')"
                    ),
                    {"uid": user_id, "org": str(org[0])},
                )

        # claims -> roles: mapped role names must exist in this org
        role_ids: list[str] = []
        skipped: list[str] = []
        for role_name in mapped["roles"]:
            found = (
                await session.execute(
                    sql("SELECT id FROM role WHERE org_id = :org AND name = :name"),
                    {"org": str(org[0]), "name": role_name},
                )
            ).first()
            if found is None:
                skipped.append(role_name)
            else:
                role_ids.append(str(found[0]))
        if role_ids:
            await assign_roles(session, user_id, str(org[0]), role_ids)
            await session.execute(
                sql(
                    "UPDATE membership SET role = :label WHERE user_id = :uid "
                    "AND org_id = :org"
                ),
                {"label": mapped["roles"][0], "uid": user_id, "org": str(org[0])},
            )
        await session.commit()
        sid = await sessions.create_session(session, user_id, external.uid)
        memberships = (
            await session.execute(
                sql(
                    "SELECT m.org_id, o.slug, o.type FROM membership m "
                    "JOIN organization o ON o.id = m.org_id "
                    "WHERE m.user_id = :uid AND m.status = 'active'"
                ),
                {"uid": user_id},
            )
        ).mappings().all()

    await request.app.state.event_bus.publish(
        "auth.oidc_sign_in",
        domain_event(
            "auth.oidc_sign_in", "organization", str(org[0]),
            {"email": external.email, "idp": config.display_name,
             "roles": mapped["roles"], "roles_skipped": skipped},
        ),
    )
    return {
        "session_id": sid,
        "email": external.email,
        "name": mapped["name"],
        "roles": mapped["roles"],
        "roles_skipped": skipped,
        "memberships": [
            {"org_id": str(m["org_id"]), "slug": m["slug"], "type": m["type"]}
            for m in memberships
        ],
    }
