"""0012 vessel types: yachts and boats in three sizes (task 31r).

Revision ID: 0012
Revises: 0011

Data-only migration. Owner feedback (23 Oct): "vessel type does not
include yachts and boats small/medium/large". Extends the identity
vessel_type catalog (0011) with six profile-only rows, idempotent
(INSERT ... ON CONFLICT DO NOTHING — a second run adds nothing).
sort_order continues after the existing nine (10-15).

RELATIONSHIP TO THE REGISTRY (documented in the 31r report): the
profile's vessel-type catalog is now a SUPERSET of registry SHIP_TYPES
(stage-3 class taxonomy). The registry's vessel.ship_type CHECK is
UNCHANGED — the registry taxonomy is the operational class vocabulary;
the identity catalog is marketing-facing and broader. Overlap = the
original nine codes (the shared vocabulary); yacht_*/boat_* are
profile-only. No registry migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# sort_order continues after the existing nine (1-9).
VESSEL_TYPES = [
    ("yacht_small", "Yacht — small", 10),
    ("yacht_medium", "Yacht — medium", 11),
    ("yacht_large", "Yacht — large", 12),
    ("boat_small", "Boat — small", 13),
    ("boat_medium", "Boat — medium", 14),
    ("boat_large", "Boat — large", 15),
]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO vessel_type (ship_type, label, sort_order) "
            "VALUES (:code, :label, :order) "
            "ON CONFLICT (ship_type) DO NOTHING"
        ),
        [{"code": c, "label": l, "order": o} for c, l, o in VESSEL_TYPES],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM vessel_type WHERE ship_type IN "
            "('yacht_small', 'yacht_medium', 'yacht_large', "
            "'boat_small', 'boat_medium', 'boat_large')"
        )
    )
