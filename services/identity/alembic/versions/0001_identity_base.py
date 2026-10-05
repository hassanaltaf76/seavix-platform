"""0001 identity base — organization, app_user, membership.

Revision ID: 0001
Revises:
Create Date: 2026-10-05

One Postgres database per service (seavix_identity) — no shared databases.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("firebase_uid", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "membership",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "org_id", sa.Uuid(), sa.ForeignKey("organization.id"), nullable=False
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "org_id"),
    )


def downgrade() -> None:
    op.drop_table("membership")
    op.drop_table("app_user")
    op.drop_table("organization")
