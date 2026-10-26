"""RBAC: default-role seeding, role CRUD, and membership role assignment.

Intra-org authorization lives in the identity database (D2: permission
catalog is system-owned; tenants compose roles from it). Effective
permissions of a membership = UNION of its roles' permissions; there is no
deny — absence of an allow means no access.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from seavix_ports import AuthContext, require_auth
from sqlalchemy import text

from .db import SessionLocal
from .permissions import DEFAULT_ROLES, Permission

router = APIRouter()

ROLE_NAME_MAX = 64


# ------------------------------------------------------------- seeding

async def seed_default_roles(session, org_id: str) -> None:
    """Insert OR refresh the five system roles for an org (idempotent by
    name).

    Called inside the org-creation transaction (registration) and at
    authentication time for existing orgs. System roles are read-only to
    tenants and their permission set is code-defined, so the catalog always
    wins: an org registered before a catalog extension (e.g. quote.view)
    converges to the current code-derived grants instead of keeping a stale
    snapshot forever. The conflict guard skips the write when already
    converged, so the steady state costs a read, not a write. Membership
    binding is separate: `assign_roles`.
    """
    for name, permissions in DEFAULT_ROLES.items():
        await session.execute(
            text(
                "INSERT INTO role (id, org_id, name, permissions, is_system) "
                "VALUES (:id, :org, :name, :perms, true) "
                "ON CONFLICT (org_id, name) DO UPDATE "
                "SET permissions = EXCLUDED.permissions "
                "WHERE role.is_system "
                "AND role.permissions IS DISTINCT FROM EXCLUDED.permissions"
            ),
            {
                "id": str(uuid.uuid4()),
                "org": org_id,
                "name": name,
                "perms": sorted(permissions),
            },
        )


# 32f: org ids this process has already converged. The permission catalog
# is code-defined and constant per deployment, so one guarded upsert per
# org per process is enough; a code upgrade restarts the process and
# re-runs the (no-op when clean) upsert. Tests call reset_converged().
_CONVERGED: set[str] = set()


async def converge_org_roles(session, org_id: str) -> bool:
    """One-shot-per-process refresh of an org's system roles to the
    code-defined catalog. Returns True when convergence actually ran (the
    caller may commit); the steady state is a cache hit, not a query."""
    if org_id in _CONVERGED:
        return False
    await seed_default_roles(session, org_id)
    _CONVERGED.add(org_id)
    return True


def reset_converged() -> None:
    _CONVERGED.clear()


async def assign_roles(session, user_id: str, org_id: str, role_ids: list[str]) -> None:
    """Replace a membership's role set (1..N)."""
    await session.execute(
        text(
            "DELETE FROM membership_role WHERE user_id = :uid AND org_id = :org"
        ),
        {"uid": user_id, "org": org_id},
    )
    for rid in role_ids:
        await session.execute(
            text(
                "INSERT INTO membership_role (user_id, org_id, role_id) "
                "VALUES (:uid, :org, :rid)"
            ),
            {"uid": user_id, "org": org_id, "rid": rid},
        )


async def role_id_by_name(session, org_id: str, name: str) -> str:
    row = (
        await session.execute(
            text("SELECT id FROM role WHERE org_id = :org AND name = :name"),
            {"org": org_id, "name": name},
        )
    ).first()
    return str(row[0])


# ------------------------------------------------------------- helpers

def _my_membership(ctx: AuthContext):
    membership = ctx.memberships[0] if ctx.memberships else None
    if membership is None:
        raise HTTPException(status_code=403, detail="no organization membership")
    return membership


def _check_perm(ctx: AuthContext, permission: str) -> AuthContext:
    membership = _my_membership(ctx)
    if permission not in membership.permissions:
        raise HTTPException(status_code=403, detail=f"permission {permission} required")
    return ctx


async def _org_scoped_roles(session, org_id: str) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT id, name, permissions, is_system, created_at FROM role "
                "WHERE org_id = :org ORDER BY is_system DESC, name"
            ),
            {"org": org_id},
        )
    ).mappings().all()
    return [
        {
            "role_id": str(r["id"]),
            "name": r["name"],
            "permissions": list(r["permissions"]),
            "is_system": bool(r["is_system"]),
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


# ------------------------------------------------------------- schemas

class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=ROLE_NAME_MAX)
    permissions: list[str] = Field(min_length=1)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=ROLE_NAME_MAX)
    permissions: list[str] | None = Field(default=None, min_length=1)


class MemberRolesUpdate(BaseModel):
    role_ids: list[str] = Field(min_length=1)


def _validate_catalog_permissions(values: list[str]) -> list[str]:
    known = {p.value for p in Permission}
    unknown = [v for v in values if v not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"permissions not in the system catalog: {sorted(set(unknown))}",
        )
    # de-duplicated, order-stable
    return list(dict.fromkeys(values))


async def _get_role_row(session, org_id: str, role_id: str) -> dict:
    row = (
        await session.execute(
            text(
                "SELECT id, name, permissions, is_system FROM role "
                "WHERE id = :id AND org_id = :org"
            ),
            {"id": role_id, "org": org_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="role not found")
    return dict(row)


# ------------------------------------------------------------- endpoints

@router.get("/api/orgs/me/roles")
async def list_roles(request: Request, ctx: AuthContext = Depends(require_auth)):
    # 31e audit fix: the roles LIST is a management surface and is gated by
    # role.manage like role create/edit/delete — not by org.profile.view
    # (which wrongly exposed the full role catalog to every profile viewer).
    _check_perm(ctx, Permission.ROLE_MANAGE.value)
    membership = _my_membership(ctx)
    async with SessionLocal() as session:
        roles = await _org_scoped_roles(session, membership.org_id)
    return {"org_id": membership.org_id, "roles": roles}


@router.post("/api/orgs/me/roles", status_code=201)
async def create_role(
    payload: RoleCreate, request: Request, ctx: AuthContext = Depends(require_auth)
):
    _check_perm(ctx, Permission.ROLE_MANAGE.value)
    membership = _my_membership(ctx)
    permissions = _validate_catalog_permissions(payload.permissions)
    name = payload.name.strip()
    async with SessionLocal() as session:
        clash = (
            await session.execute(
                text("SELECT 1 FROM role WHERE org_id = :org AND name = :name"),
                {"org": membership.org_id, "name": name},
            )
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="role name already exists")
        role_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO role (id, org_id, name, permissions, is_system) "
                "VALUES (:id, :org, :name, :perms, false)"
            ),
            {"id": role_id, "org": membership.org_id, "name": name, "perms": permissions},
        )
        await session.commit()
    return {
        "role_id": role_id,
        "name": name,
        "permissions": permissions,
        "is_system": False,
    }


@router.put("/api/orgs/me/roles/{role_id}")
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_auth),
):
    _check_perm(ctx, Permission.ROLE_MANAGE.value)
    membership = _my_membership(ctx)
    async with SessionLocal() as session:
        row = await _get_role_row(session, membership.org_id, role_id)
        if row["is_system"]:
            raise HTTPException(status_code=409, detail="system roles are read-only")
        name = payload.name.strip() if payload.name is not None else row["name"]
        permissions = (
            _validate_catalog_permissions(payload.permissions)
            if payload.permissions is not None
            else list(row["permissions"])
        )
        if name != row["name"]:
            clash = (
                await session.execute(
                    text(
                        "SELECT 1 FROM role WHERE org_id = :org AND name = :name "
                        "AND id <> :id"
                    ),
                    {"org": membership.org_id, "name": name, "id": role_id},
                )
            ).first()
            if clash:
                raise HTTPException(status_code=409, detail="role name already exists")
        await session.execute(
            text(
                "UPDATE role SET name = :name, permissions = :perms "
                "WHERE id = :id AND org_id = :org"
            ),
            {"name": name, "perms": permissions, "id": role_id, "org": membership.org_id},
        )
        await session.commit()
    return {"role_id": role_id, "name": name, "permissions": permissions,
            "is_system": False}


@router.delete("/api/orgs/me/roles/{role_id}")
async def delete_role(
    role_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    _check_perm(ctx, Permission.ROLE_MANAGE.value)
    membership = _my_membership(ctx)
    async with SessionLocal() as session:
        row = await _get_role_row(session, membership.org_id, role_id)
        if row["is_system"]:
            raise HTTPException(status_code=409, detail="system roles are read-only")
        assigned = (
            await session.execute(
                text("SELECT 1 FROM membership_role WHERE role_id = :id LIMIT 1"),
                {"id": role_id},
            )
        ).first()
        if assigned:
            raise HTTPException(
                status_code=409, detail="role is assigned to members — reassign first"
            )
        await session.execute(
            text("DELETE FROM role WHERE id = :id AND org_id = :org"),
            {"id": role_id, "org": membership.org_id},
        )
        await session.commit()
    return {"deleted": role_id}


@router.get("/api/orgs/me/members/{user_id}/roles")
async def get_member_roles(
    user_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    _check_perm(ctx, Permission.MEMBER_MANAGE.value)
    membership = _my_membership(ctx)
    async with SessionLocal() as session:
        target = (
            await session.execute(
                text(
                    "SELECT 1 FROM membership WHERE user_id = :uid AND org_id = :org"
                ),
                {"uid": user_id, "org": membership.org_id},
            )
        ).first()
        if target is None:
            raise HTTPException(status_code=404, detail="member not found")
        rows = (
            await session.execute(
                text(
                    "SELECT r.id, r.name, r.permissions FROM membership_role mr "
                    "JOIN role r ON r.id = mr.role_id "
                    "WHERE mr.user_id = :uid AND mr.org_id = :org ORDER BY r.name"
                ),
                {"uid": user_id, "org": membership.org_id},
            )
        ).mappings().all()
    effective: set[str] = set()
    for r in rows:
        effective |= set(r["permissions"])
    return {
        "user_id": user_id,
        "org_id": membership.org_id,
        "roles": [
            {"role_id": str(r["id"]), "name": r["name"],
             "permissions": list(r["permissions"])}
            for r in rows
        ],
        "effective_permissions": sorted(effective),
    }


@router.put("/api/orgs/me/members/{user_id}/roles")
async def set_member_roles(
    user_id: str,
    payload: MemberRolesUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_auth),
):
    """Replace a member's role set (1..N)."""
    _check_perm(ctx, Permission.MEMBER_MANAGE.value)
    membership = _my_membership(ctx)
    async with SessionLocal() as session:
        target = (
            await session.execute(
                text(
                    "SELECT 1 FROM membership WHERE user_id = :uid AND org_id = :org"
                ),
                {"uid": user_id, "org": membership.org_id},
            )
        ).first()
        if target is None:
            raise HTTPException(status_code=404, detail="member not found")
        for rid in payload.role_ids:
            owned = (
                await session.execute(
                    text(
                        "SELECT 1 FROM role WHERE id = :id AND org_id = :org"
                    ),
                    {"id": rid, "org": membership.org_id},
                )
            ).first()
            if owned is None:
                raise HTTPException(status_code=404, detail=f"role not found: {rid}")
        await assign_roles(session, user_id, membership.org_id, payload.role_ids)
        # keep the legacy denormalized label roughly in sync (display only)
        first = (
            await session.execute(
                text("SELECT name FROM role WHERE id = :id"), {"id": payload.role_ids[0]}
            )
        ).first()
        await session.execute(
            text(
                "UPDATE membership SET role = :label "
                "WHERE user_id = :uid AND org_id = :org"
            ),
            {"label": first[0] if first else "member", "uid": user_id,
             "org": membership.org_id},
        )
        await session.commit()
    return {"user_id": user_id, "role_ids": payload.role_ids}
