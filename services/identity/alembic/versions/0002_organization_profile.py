"""0002 organization profile columns.

Revision ID: 0002
Revises: 0001

Adds public-profile fields to organization. Slug format is enforced in the
API layer and by a DB CHECK (Postgres regex operator; SQLite would not
accept this form, but this stack runs Postgres only — documented).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("slug", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("website", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("phone", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("about", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("logo_blob_key", sa.Text(), nullable=True))
    op.add_column(
        "organization",
        sa.Column(
            "profile_status",
            sa.Text(),
            nullable=False,
            server_default="draft",
        ),
    )

    # Backfill: existing rows get a deterministic unique slug from their id.
    op.execute(
        "UPDATE organization SET slug = 'org-' || lower(id::text) WHERE slug IS NULL"
    )

    op.alter_column("organization", "slug", nullable=False)
    op.create_unique_constraint("uq_organization_slug", "organization", ["slug"])
    op.create_check_constraint(
        "ck_organization_slug_format",
        "organization",
        "slug ~ '^[a-z0-9-]+$'",
    )
    op.create_check_constraint(
        "ck_organization_profile_status",
        "organization",
        "profile_status IN ('draft', 'live', 'suspended')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_organization_profile_status", "organization")
    op.drop_constraint("ck_organization_slug_format", "organization")
    op.drop_constraint("uq_organization_slug", "organization")
    op.alter_column("organization", "slug", nullable=True)
    op.drop_column("organization", "profile_status")
    op.drop_column("organization", "logo_blob_key")
    op.drop_column("organization", "about")
    op.drop_column("organization", "phone")
    op.drop_column("organization", "website")
    op.drop_column("organization", "slug")
