"""0001 work base — RFQ → quote → job spine.

Revision ID: 0001
Revises:

Documented schema decisions (slice1-spine-spec):
- rfq.requesting_org_id is NULL for anonymous public intake; converting
  anonymous requesters into registered client orgs is a later slice.
- billing_org_id defaults to requesting_org_id when set (API layer).
- vessel_imo is plain TEXT — registry is a separate service; cross-service
  joins happen at read time, never via DB foreign keys.
- quote.line_items is JSONB; a sent quote is never edited (new revision
  supersedes), enforced by the API layer (no update path exists).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rfq",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requesting_org_id", sa.Uuid(), nullable=True),
        sa.Column("billing_org_id", sa.Uuid(), nullable=True),
        sa.Column("target_org_id", sa.Uuid(), nullable=False),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column("vessel_imo", sa.Text(), nullable=True),
        sa.Column("survey_type", sa.Text(), nullable=False),
        sa.Column("location_type", sa.Text(), nullable=False),
        sa.Column("preferred_date", sa.Date(), nullable=True),
        sa.Column("scope_notes", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="internal"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('public', 'internal')", name="ck_rfq_source"),
        sa.CheckConstraint(
            "status IN ('open', 'quoted', 'accepted', 'declined', 'expired')",
            name="ck_rfq_status",
        ),
    )

    op.create_table(
        "quote",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("rfq_id", sa.Uuid(), sa.ForeignKey("rfq.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("line_items", JSONB(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("rfq_id", "revision", name="uq_quote_rfq_revision"),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'accepted', 'declined', 'superseded')",
            name="ck_quote_status",
        ),
    )

    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("rfq_id", sa.Uuid(), sa.ForeignKey("rfq.id"), nullable=False),
        sa.Column("quote_id", sa.Uuid(), sa.ForeignKey("quote.id"), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("survey_type", sa.Text(), nullable=False),
        sa.Column("vessel_imo", sa.Text(), nullable=True),
        sa.Column("requesting_org_id", sa.Uuid(), nullable=True),
        sa.Column("billing_org_id", sa.Uuid(), nullable=True),
        sa.Column("owner_org_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("type IN ('survey', 'bunker')", name="ck_job_type"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'in_progress', 'awaiting_review', 'approved', "
            "'reported', 'invoiced', 'closed')",
            name="ck_job_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("job")
    op.drop_table("quote")
    op.drop_table("rfq")
