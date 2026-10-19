"""0005 membership status + invites.

Revision ID: 0005
Revises: 0004

- membership.status: active | deactivated. Deactivation preserves history
  (membership + membership_role rows stay); authorization layers ignore
  deactivated memberships and deactivation revokes live sessions.
- invite: invite-based user creation (D4 — admins never set passwords).
  Single-use token, 24h TTL, hashed at rest; role assignments ride along.
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "membership",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_check_constraint(
        "ck_membership_status",
        "membership",
        "status IN ('active', 'deactivated')",
    )

    op.create_table(
        "invite",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role_ids", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("invited_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_invite_org", "invite", ["org_id"])
    op.create_index("ix_invite_email", "invite", ["email"])


def downgrade() -> None:
    op.drop_index("ix_invite_email", table_name="invite")
    op.drop_index("ix_invite_org", table_name="invite")
    op.drop_table("invite")
    op.drop_constraint("ck_membership_status", "membership")
    op.drop_column("membership", "status")
