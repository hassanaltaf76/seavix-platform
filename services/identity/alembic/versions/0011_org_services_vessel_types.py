"""0011 org services + vessel types (task 31q).

Revision ID: 0011
Revises: 0010

System catalogs and org selections for the two new profile fields:

- service (service_code TEXT PK, label TEXT, sort_order INT): the survey-
  service catalog. Seeded with the ten owner-listed services, idempotent
  (INSERT ... ON CONFLICT DO UPDATE — re-running upgrade refreshes labels
  and order, never duplicates).
- org_service (org_id -> organization, service_code TEXT, PK both):
  a survey company's published services. service_code is a code reference
  to the identity-owned catalog — plain TEXT with NO database FK, the same
  pattern as org_port (identity owns the reference data this time).
- vessel_type (ship_type TEXT PK, label TEXT, sort_order INT): the
  stage-3 ship taxonomy — the SAME nine codes registry's vessel.taxonomy
  uses (oil_tanker ... tug); identity carries its own catalog copy so the
  public read model can join label by code without a registry round trip.
  Seeded idempotent, labels humanized.
- org_vessel_type (org_id -> organization, ship_type TEXT, PK both): same
  shape as org_service.

Catalog admin-editing is a LATER slice (documented in the 31q spec
amendment, deliberately not built here).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SERVICES = [
    ("pre_purchase_survey", "Pre-purchase survey", 1),
    ("condition_survey", "Condition survey", 2),
    ("bunker_quantity_survey", "Bunker quantity survey", 3),
    ("loading_discharging_attendance", "Loading/discharging attendance", 4),
    ("on_hire_off_hire_survey", "On-hire/off-hire survey", 5),
    ("draft_survey", "Draft survey", 6),
    ("cargo_hold_condition", "Cargo hold condition", 7),
    ("magnetic_compass_adjustment", "Magnetic compass adjustment", 8),
    ("hot_work_gas_free_certificate", "Hot work / gas-free certificate", 9),
    ("third_party_inspection", "Third-party inspection", 10),
]

# The shared vocabulary with registry's vessel.taxonomy (vessels.py
# SHIP_TYPES, same order) — joined at read time by code, no FK either side.
VESSEL_TYPES = [
    ("oil_tanker", "Oil tanker", 1),
    ("chemical_tanker", "Chemical tanker", 2),
    ("gas_carrier", "Gas carrier", 3),
    ("bulk_carrier", "Bulk carrier", 4),
    ("general_cargo", "General cargo", 5),
    ("container", "Container", 6),
    ("passenger", "Passenger", 7),
    ("offshore_supply", "Offshore supply", 8),
    ("tug", "Tug", 9),
]


def upgrade() -> None:
    op.create_table(
        "service",
        sa.Column("service_code", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "org_service",
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organization.id"),
            nullable=False,
        ),
        sa.Column("service_code", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("org_id", "service_code"),
    )
    op.create_index("ix_org_service_org", "org_service", ["org_id"])

    op.create_table(
        "vessel_type",
        sa.Column("ship_type", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "org_vessel_type",
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organization.id"),
            nullable=False,
        ),
        sa.Column("ship_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("org_id", "ship_type"),
    )
    op.create_index("ix_org_vessel_type_org", "org_vessel_type", ["org_id"])

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO service (service_code, label, sort_order) "
            "VALUES (:code, :label, :order) "
            "ON CONFLICT (service_code) DO UPDATE SET "
            "label = EXCLUDED.label, sort_order = EXCLUDED.sort_order"
        ),
        [{"code": c, "label": l, "order": o} for c, l, o in SERVICES],
    )
    conn.execute(
        sa.text(
            "INSERT INTO vessel_type (ship_type, label, sort_order) "
            "VALUES (:code, :label, :order) "
            "ON CONFLICT (ship_type) DO UPDATE SET "
            "label = EXCLUDED.label, sort_order = EXCLUDED.sort_order"
        ),
        [{"code": c, "label": l, "order": o} for c, l, o in VESSEL_TYPES],
    )


def downgrade() -> None:
    op.drop_index("ix_org_vessel_type_org", table_name="org_vessel_type")
    op.drop_table("org_vessel_type")
    op.drop_table("vessel_type")
    op.drop_index("ix_org_service_org", table_name="org_service")
    op.drop_table("org_service")
    op.drop_table("service")
