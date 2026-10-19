"""Tenant user management: invite-based member creation (D4), member
listing, and deactivate/reactivate.

Invite lifecycle: an inviter with member.invite posts an email + 1..N role
ids -> single-use token (24h TTL, hashed at rest) -> the invitee accepts by
setting their password ( Firebase user + app_user + membership created ),
gets a verification code emailed (dev: logged), verifies, and logs in with
the standing OTP challenge.

Cross-tenant policy: every query here is org-scoped; a resource belonging
to another org is a 404, never a 403 (404-over-403, documented).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from seavix_ports import AuthContext, require_auth
from sqlalchemy import JSON, bindparam, text as sql

from seavix_ports import domain_event, public_route

from . import config, sessions
from .db import SessionLocal
from .permissions import Permission
from .rbac import assign_roles, role_id_by_name
from .register import _create_firebase_user
from .security import require_permission

logger = logging.getLogger("seavix.identity.members")

router = APIRouter()

INVITE_TTL = timedelta(hours=24)
# Dev-only plaintext invite cache (flag-gated, memory-only) — same pattern
# as the OTP dev peek.
_DEV_INVITES: dict[str, str] = {}


def _hash_token(token: str) -> str:
    return hashlib.sha256(f"invite:{token}:{config.otp_pepper()}".encode()).hexdigest()


def _my_membership(ctx: AuthContext):
    membership = ctx.memberships[0] if ctx.memberships else None
    if membership is None:
        raise HTTPException(status_code=403, detail="no organization membership")
    return membership


class InviteCreate(BaseModel):
    email: EmailStr
    role_ids: list[str] = Field(min_length=1)


class InviteAccept(BaseModel):
    token: str = Field(min_length=16)
    password: str = Field(min_length=6, max_length=128)


@router.post("/api/orgs/me/invites", status_code=201)
async def create_invite(
    payload: InviteCreate, request: Request,
    ctx: AuthContext = Depends(require_auth),
):
    membership = require_permission(ctx, Permission.MEMBER_INVITE.value)
    org_id = membership.org_id
    email = payload.email.lower()

    async with SessionLocal() as session:
        inviter = (
            await session.execute(
                sql("SELECT id FROM app_user WHERE firebase_uid = :uid"),
                {"uid": ctx.user.uid},
            )
        ).scalar_one()
        existing = (
            await session.execute(
                sql(
                    "SELECT 1 FROM app_user u JOIN membership m "
                    "ON m.user_id = u.id AND m.org_id = :org "
                    "WHERE u.email = :email"
                ),
                {"org": org_id, "email": email},
            )
        ).first()
        if existing:
            raise HTTPException(
                status_code=409, detail="this email is already a member"
            )
        for rid in payload.role_ids:
            role = (
                await session.execute(
                    sql("SELECT 1 FROM role WHERE id = :id AND org_id = :org"),
                    {"id": rid, "org": org_id},
                )
            ).first()
            if role is None:
                raise HTTPException(status_code=404, detail="role not found")

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        invite_id = str(uuid.uuid4())
        insert = sql(
            "INSERT INTO invite (id, org_id, email, role_ids, token_hash, "
            "invited_by, created_at, expires_at) "
            "VALUES (:id, :org, :email, :roles, :hash, :by, :now, :exp)"
        ).bindparams(bindparam("roles", type_=JSON))
        await session.execute(
            insert,
            {
                "id": invite_id,
                "org": org_id,
                "email": email,
                "roles": payload.role_ids,
                "hash": _hash_token(token),
                "by": str(inviter),
                "now": now,
                "exp": now + INVITE_TTL,
            },
        )
        await session.commit()

    # Dev delivery channel (prod: email — TODO). The link is API-only in
    # this scaffold, so the token IS the link payload.
    logger.info("DEV-INVITE email=%s org=%s token=%s", email, org_id, token)
    if config.dev_otp_endpoint_enabled():
        _DEV_INVITES[email] = token
    await request.app.state.event_bus.publish(
        "member.invited",
        domain_event("member.invited", "organization", org_id,
                     {"email": email, "invite_id": invite_id}),
    )
    return {"invite_id": invite_id, "email": email,
            "expires_at": (now + INVITE_TTL).isoformat()}


@router.get("/api/orgs/me/invites")
async def list_invites(
    request: Request, ctx: AuthContext = Depends(require_auth)
):
    membership = require_permission(ctx, Permission.MEMBER_MANAGE.value)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                sql(
                    "SELECT id, email, role_ids, created_at, expires_at, "
                    "accepted_at, revoked_at FROM invite "
                    "WHERE org_id = :org ORDER BY created_at DESC"
                ),
                {"org": membership.org_id},
            )
        ).mappings().all()
    return {
        "org_id": membership.org_id,
        "invites": [
            {
                "invite_id": str(r["id"]),
                "email": r["email"],
                "role_ids": list(r["role_ids"]),
                "created_at": r["created_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
                "accepted_at": r["accepted_at"].isoformat() if r["accepted_at"] else None,
                "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/api/public/invites/accept", status_code=201)
@public_route
async def accept_invite(payload: InviteAccept, request: Request) -> dict:
    """PUBLIC (the invitee has no credentials yet). Generic 404 on any
    invalid/expired/consumed/revoked token — enumeration reveals nothing."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                sql(
                    "SELECT id, org_id, email, role_ids, expires_at, "
                    "accepted_at, revoked_at FROM invite "
                    "WHERE token_hash = :hash"
                ),
                {"hash": _hash_token(payload.token)},
            )
        ).mappings().first()
        now = datetime.now(timezone.utc)
        if row is None:
            raise HTTPException(status_code=404, detail="invalid or expired invite")
        for key in ("expires_at",):
            value = row[key]
            if value.tzinfo is None:
                row[key] = value.replace(tzinfo=timezone.utc)
        if row["accepted_at"] is not None or row["revoked_at"] is not None:
            raise HTTPException(status_code=404, detail="invalid or expired invite")
        if now > row["expires_at"]:
            raise HTTPException(status_code=404, detail="invalid or expired invite")

        fb = await _create_firebase_user(row["email"], payload.password)
        user_id = str(uuid.uuid4())
        await session.execute(
            sql(
                "INSERT INTO app_user (id, email, firebase_uid) "
                "VALUES (:id, :email, :fb)"
            ),
            {"id": user_id, "email": row["email"], "fb": fb["localId"]},
        )
        await session.execute(
            sql(
                "INSERT INTO membership (user_id, org_id, role, status) "
                "VALUES (:uid, :org, 'member', 'active')"
            ),
            {"uid": user_id, "org": str(row["org_id"])},
        )
        await assign_roles(session, user_id, str(row["org_id"]), list(row["role_ids"]))
        await session.execute(
            sql("UPDATE invite SET accepted_at = :now WHERE id = :id"),
            {"now": now, "id": row["id"]},
        )
        await session.commit()

    # Email verification is part of becoming an active member: send the
    # Admin SDK verification link through the SeaVix mail pipeline.
    from firebase_admin import auth

    from .mail_templates import email_verification_email
    from .mailer import get_mailer
    from .security import _ensure_firebase

    from .authn import _oob_code, _web_link

    _ensure_firebase()
    action = auth.generate_email_verification_link(row["email"])
    link = _web_link("/register/verify/confirm", email=row["email"],
                     oob_code=_oob_code(action))
    subject, plain, html = email_verification_email(link)
    await get_mailer().send(row["email"], subject, plain, html)

    await request.app.state.event_bus.publish(
        "member.joined",
        domain_event("member.joined", "organization", str(row["org_id"]),
                     {"email": row["email"], "user_id": user_id}),
    )
    return {
        "org_id": str(row["org_id"]),
        "email": row["email"],
        "member": True,
        "verification_required": True,
    }


@router.get("/api/orgs/me/members")
async def list_members(
    request: Request, ctx: AuthContext = Depends(require_auth)
):
    membership = require_permission(ctx, Permission.MEMBER_MANAGE.value)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                sql(
                    "SELECT u.id, u.email, u.email_verified, m.status "
                    "FROM membership m JOIN app_user u ON u.id = m.user_id "
                    "WHERE m.org_id = :org ORDER BY u.email"
                ),
                {"org": membership.org_id},
            )
        ).mappings().all()
        out = []
        for r in rows:
            role_rows = (
                await session.execute(
                    sql(
                        "SELECT r.name, r.permissions FROM membership_role mr "
                        "JOIN role r ON r.id = mr.role_id "
                        "WHERE mr.user_id = :uid AND mr.org_id = :org"
                    ),
                    {"uid": str(r["id"]), "org": membership.org_id},
                )
            ).mappings().all()
            effective: set[str] = set()
            for x in role_rows:
                effective |= set(x["permissions"])
            out.append(
                {
                    "user_id": str(r["id"]),
                    "email": r["email"],
                    "email_verified": bool(r["email_verified"]),
                    "status": r["status"],
                    "roles": [x["name"] for x in role_rows],
                    "effective_permissions": sorted(effective),
                }
            )
    return {"org_id": membership.org_id, "members": out}


async def _target_membership(session, org_id: str, user_id: str) -> None:
    found = (
        await session.execute(
            sql(
                "SELECT 1 FROM membership WHERE user_id = :uid AND org_id = :org"
            ),
            {"uid": user_id, "org": org_id},
        )
    ).first()
    if found is None:
        # 404-over-403: another org's member is indistinguishable from a
        # nonexistent one.
        raise HTTPException(status_code=404, detail="member not found")


@router.post("/api/orgs/me/members/{user_id}/deactivate")
async def deactivate_member(
    user_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    membership = require_permission(ctx, Permission.MEMBER_MANAGE.value)
    async with SessionLocal() as session:
        await _target_membership(session, membership.org_id, user_id)
        await session.execute(
            sql(
                "UPDATE membership SET status = 'deactivated' "
                "WHERE user_id = :uid AND org_id = :org"
            ),
            {"uid": user_id, "org": membership.org_id},
        )
        revoked = await sessions.revoke_all_for_user(session, user_id)
        await session.commit()
    await request.app.state.event_bus.publish(
        "member.deactivated",
        domain_event("member.deactivated", "organization", membership.org_id,
                     {"user_id": user_id, "sessions_revoked": revoked}),
    )
    return {"user_id": user_id, "status": "deactivated",
            "sessions_revoked": revoked}


@router.post("/api/orgs/me/members/{user_id}/reactivate")
async def reactivate_member(
    user_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    membership = require_permission(ctx, Permission.MEMBER_MANAGE.value)
    async with SessionLocal() as session:
        await _target_membership(session, membership.org_id, user_id)
        await session.execute(
            sql(
                "UPDATE membership SET status = 'active' "
                "WHERE user_id = :uid AND org_id = :org"
            ),
            {"uid": user_id, "org": membership.org_id},
        )
        await session.commit()
    await request.app.state.event_bus.publish(
        "member.reactivated",
        domain_event("member.reactivated", "organization", membership.org_id,
                     {"user_id": user_id}),
    )
    return {"user_id": user_id, "status": "active"}


# --------------------------------------------------------- dev invite peek

@router.get("/api/dev/last-invite")
@public_route
async def dev_last_invite(email: str, request: Request) -> dict:
    """DEV ONLY (SEAVIX_DEV_OTP_ENDPOINT=1): the last invite token for an
    email. Memory-only, same flag as the OTP peek. Inert (404) without it."""
    if not config.dev_otp_endpoint_enabled():
        raise HTTPException(status_code=404, detail="not found")
    token = _DEV_INVITES.get(email.strip().lower())
    if token is None:
        raise HTTPException(status_code=404, detail="no invite for email")
    return {"email": email.lower(), "token": token}
