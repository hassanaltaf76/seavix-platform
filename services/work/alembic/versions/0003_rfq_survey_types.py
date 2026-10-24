"""0003 RFQ survey types (task 32d): multi-select, catalog-backed.

Revision ID: 0003
Revises: 0002

The RFQ form's survey_type becomes a catalog-backed chip picker (identity
owns the survey_type catalog; system seeds + harvested custom entries).
Selections are ORG-INDEPENDENT: any catalog code may be chosen, not just
the ones on the org's profile — hence a join on the RFQ, not the org.

- rfq_survey_type (rfq_id -> rfq, survey_type TEXT, PK both): survey_type
  is a code reference to IDENTITY's catalog — plain TEXT with NO cross-DB
  FK (same rule as rfq.*_org_id and client_directory.client_org_id).
- The legacy rfq.survey_type column stays: it now holds the FIRST selected
  code (the "primary" survey type) so existing readers keep working while
  the multi-select list is the authoritative storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rfq_survey_type",
        sa.Column("rfq_id", sa.Uuid(), sa.ForeignKey("rfq.id"),
                  nullable=False),
        sa.Column("survey_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("rfq_id", "survey_type"),
    )
    op.create_index("ix_rfq_survey_type_rfq", "rfq_survey_type", ["rfq_id"])


def downgrade() -> None:
    op.drop_index("ix_rfq_survey_type_rfq", table_name="rfq_survey_type")
    op.drop_table("rfq_survey_type")
