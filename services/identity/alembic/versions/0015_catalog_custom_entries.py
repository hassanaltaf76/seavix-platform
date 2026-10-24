"""0015 custom catalog entries + survey_type catalog (task 32d).

Revision ID: 0015
Revises: 0014

Owner spec (24 Oct): "survey type, services, vessel types and similar
fields must allow additional free text; the platform harvests them and
they show up in future lists".

- service and vessel_type gain provenance columns: origin
  ('system'|'custom', NOT NULL DEFAULT 'system'), created_by_org_id
  (UUID NULL — NULL for system rows and anonymous public-intake creates),
  created_at. A catalog row IS the harvest: any org's picker search
  surfaces custom rows (system first, custom after, "custom" badge in the
  dropdown). Promote/merge curation is stage-16 scope — documented, not
  built.
- survey_type: NEW identity-owned catalog (same shape as service) seeded
  with the four RFQ survey types, plus org_survey_type — the profile's
  multi-select join, same pattern as org_service/org_vessel_type.
- PORTS ARE EXCLUDED from this mechanism: UN/LOCODE is external
  reference data owned by the registry, not our taxonomy — the ports
  picker never offers a create option (contract-tested).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SURVEY_TYPES = [
    ("pre_purchase", "Pre-purchase survey", 1),
    ("condition", "Condition survey", 2),
    ("bunker_quantity", "Bunker quantity survey", 3),
    ("loading_discharging", "Loading/discharging attendance", 4),
]

_ORIGIN_CHECK = "origin IN ('system', 'custom')"


def _add_provenance(table: str) -> None:
    op.add_column(table, sa.Column("origin", sa.Text(), nullable=False,
                                   server_default="system"))
    op.add_column(table, sa.Column("created_by_org_id", sa.Uuid(),
                                   nullable=True))
    op.add_column(table, sa.Column(
        "created_at", sa.DateTime(timezone=True),
        server_default=sa.func.now(), nullable=False))
    op.create_check_constraint(f"ck_{table}_origin", table, _ORIGIN_CHECK)


def upgrade() -> None:
    _add_provenance("service")
    _add_provenance("vessel_type")

    op.create_table(
        "survey_type",
        sa.Column("survey_type_code", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False,
                  server_default="system"),
        sa.Column("created_by_org_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(_ORIGIN_CHECK, name="ck_survey_type_origin"),
    )
    op.create_table(
        "org_survey_type",
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organization.id"),
                  nullable=False),
        sa.Column("survey_type_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("org_id", "survey_type_code"),
    )
    op.create_index("ix_org_survey_type_org", "org_survey_type", ["org_id"])

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO survey_type (survey_type_code, label, sort_order) "
            "VALUES (:code, :label, :order) "
            "ON CONFLICT (survey_type_code) DO UPDATE SET "
            "label = EXCLUDED.label, sort_order = EXCLUDED.sort_order"
        ),
        [{"code": c, "label": l, "order": o} for c, l, o in SURVEY_TYPES],
    )


def downgrade() -> None:
    op.drop_index("ix_org_survey_type_org", table_name="org_survey_type")
    op.drop_table("org_survey_type")
    op.drop_table("survey_type")
    op.drop_column("vessel_type", "created_at")
    op.drop_column("vessel_type", "created_by_org_id")
    op.drop_column("vessel_type", "origin")
    op.drop_column("service", "created_at")
    op.drop_column("service", "created_by_org_id")
    op.drop_column("service", "origin")
