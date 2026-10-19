"""0008 org ports of operation (task 31f).

Revision ID: 0008
Revises: 0007

org_port: the ports of operation a survey company publishes on its public
page. port_code is a cross-service reference to registry.port.code
(UN/LOCODE) — plain TEXT with NO database FK, the same pattern as
vessel_imo (registry owns the reference data; identity only stores codes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_port",
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organization.id"),
            nullable=False,
        ),
        sa.Column("port_code", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("org_id", "port_code"),
    )
    op.create_index("ix_org_port_org", "org_port", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_org_port_org", table_name="org_port")
    op.drop_table("org_port")
