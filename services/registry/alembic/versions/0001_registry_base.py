"""0001 registry base — vessel taxonomy + ports.

Revision ID: 0001
Revises:

Vessel IMO is TEXT with a 7-digit CHECK (IMO numbers are identifiers, not
quantities — never arithmetic). port.code holds the 5-character UN/LOCODE
(e.g. AEFJR); country is stored separately.
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
        "vessel",
        sa.Column("imo", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cargo_class", sa.Text(), nullable=False),
        sa.Column("ship_type", sa.Text(), nullable=False),
        sa.Column("size_class", sa.Text(), nullable=False),
        sa.Column("dwt", sa.Numeric(), nullable=True),
        sa.Column("loa_m", sa.Numeric(), nullable=True),
        sa.Column("pump_type", sa.Text(), nullable=True),
        sa.Column("flag", sa.Text(), nullable=True),
        sa.Column("year_built", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("imo ~ '^[0-9]{7}$'", name="ck_vessel_imo_format"),
        sa.CheckConstraint(
            "cargo_class IN ('liquid', 'dry')", name="ck_vessel_cargo_class"
        ),
        sa.CheckConstraint(
            "ship_type IN ('oil_tanker', 'chemical_tanker', 'gas_carrier', "
            "'bulk_carrier', 'general_cargo', 'container', 'passenger', "
            "'offshore_supply', 'tug')",
            name="ck_vessel_ship_type",
        ),
        sa.CheckConstraint(
            "size_class IN ('handysize', 'handamax', 'panamax', 'aframax', "
            "'suezmax', 'capesize', 'other')",
            name="ck_vessel_size_class",
        ),
        sa.CheckConstraint(
            "pump_type IN ('steam_turbine', 'framo', 'screw', 'none') "
            "OR pump_type IS NULL",
            name="ck_vessel_pump_type",
        ),
    )

    op.create_table(
        "port",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("port")
    op.drop_table("vessel")
