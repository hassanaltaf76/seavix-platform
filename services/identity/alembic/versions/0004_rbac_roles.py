"""0004 RBAC: roles + multi-role memberships.

Revision ID: 0004
Revises: 0003

- role: per-org roles composed from the SYSTEM permission catalog (D2).
  is_system rows are seeded per org and are read-only via the API.
- membership_role: 1..N role assignments per membership; effective
  permissions = UNION across assigned roles (deny never overrides allow).

Data migration: existing orgs get the five default system roles and every
legacy membership (always an owner from registration) is bound to its
org's owner role.

NOTE (31e): this file's DEFAULT_ROLE_PERMISSIONS mirrors the seed catalog
for FRESH databases. The surveyor/viewer entries were reduced to
rfq.view + job.view in 31e (no org.profile.view — the profile blade is
gated to ops-and-above). Already-applied databases keep the rows they
were seeded with; system roles are read-only via the API, so pre-31e dev
databases keep the old surveyor/viewer permissions until reseeded (dev
data is throwaway — no data migration was built for it).
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_ROLE_PERMISSIONS = {
    "owner": None,  # filled at runtime from the catalog (all permissions)
    "admin": [
        "org.profile.view", "org.profile.edit", "org.logo.upload",
        "org.profile.publish", "member.invite", "member.manage",
        "rfq.create", "rfq.view", "quote.create", "quote.send",
        "quote.accept", "job.view", "job.assign",
    ],
    "ops": [
        "org.profile.view", "rfq.create", "rfq.view", "quote.create",
        "quote.send", "quote.accept", "job.view", "job.assign",
    ],
    "surveyor": ["rfq.view", "job.view"],
    "viewer": ["rfq.view", "job.view"],
}
ALL_PERMISSIONS = sorted(DEFAULT_ROLE_PERMISSIONS["admin"]) + ["role.manage"]


def upgrade() -> None:
    op.create_table(
        "role",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("permissions", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_role_org_name"),
    )
    op.create_index("ix_role_org", "role", ["org_id"])

    op.create_table(
        "membership_role",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), sa.ForeignKey("role.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"], ["membership.user_id", "membership.org_id"]
        ),
        sa.PrimaryKeyConstraint("user_id", "org_id", "role_id"),
    )
    op.create_index("ix_membership_role_role", "membership_role", ["role_id"])

    # ---- data migration: seed default roles for existing orgs and bind
    # legacy memberships (all owners) to their org's owner role.
    conn = op.get_bind()
    org_ids = [r[0] for r in conn.execute(sa.text("SELECT id FROM organization"))]
    for org_id in org_ids:
        role_ids = {}
        for name in ("owner", "admin", "ops", "surveyor", "viewer"):
            perms = ALL_PERMISSIONS if name == "owner" else DEFAULT_ROLE_PERMISSIONS[name]
            rid = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO role (id, org_id, name, permissions, is_system) "
                    "VALUES (:id, :org, :name, :perms, true)"
                ),
                {"id": str(rid), "org": str(org_id), "name": name, "perms": perms},
            )
            role_ids[name] = rid
        conn.execute(
            sa.text(
                "INSERT INTO membership_role (user_id, org_id, role_id) "
                "SELECT m.user_id, m.org_id, :rid FROM membership m "
                "WHERE m.org_id = :org"
            ),
            {"rid": str(role_ids["owner"]), "org": str(org_id)},
        )


def downgrade() -> None:
    op.drop_index("ix_membership_role_role", table_name="membership_role")
    op.drop_table("membership_role")
    op.drop_index("ix_role_org", table_name="role")
    op.drop_table("role")
