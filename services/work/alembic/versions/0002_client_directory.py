"""0002 client directory (32a): tenant ↔ client edges.

Revision ID: 0002
Revises: 0001

A client IS an organization. The edge links the TENANT org (the directory
owner — a management company / agent) to a CLIENT org (created unclaimed
in identity via POST /api/internal/orgs; claiming by self-registration is
a later slice). relationship_type is plain TEXT with a CHECK constraint —
the work service owns this vocabulary.

Documented decisions:
- No DB foreign keys across services: client_org_id references identity's
  organization by UUID only (same rule as rfq.*_org_id — cross-service
  joins happen at read time via identity's internal endpoints).
- UNIQUE(tenant_org_id, client_org_id): one edge per tenant+client pair;
  the API surfaces a violation as 409.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_directory",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_org_id", sa.Uuid(), nullable=False),
        sa.Column("client_org_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_org_id", "client_org_id", name="uq_client_directory_pair"
        ),
        sa.CheckConstraint(
            "relationship_type IN "
            "('management', 'surveyor_subcontract', 'port_agent')",
            name="ck_client_directory_relationship",
        ),
    )
    op.create_index(
        "ix_client_directory_tenant", "client_directory", ["tenant_org_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_client_directory_tenant", table_name="client_directory")
    op.drop_table("client_directory")
