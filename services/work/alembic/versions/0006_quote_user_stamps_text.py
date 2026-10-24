"""0006 quote user stamps as text (task 32c fix).

Revision ID: 0006
Revises: 0005

32c end-to-end verification caught a type defect: quote.prepared_by /
approved_by / issued_by were created as UUID in 0005, but the user
identifier services actually hold is the Firebase uid from identity's
auth-context — a 28-character opaque string, not a UUID. Every quote
write through real auth therefore died with "invalid input for type
uuid" inside work.

The Firebase uid is the canonical cross-service user identity (it is
what /api/internal/auth-context returns top-level, and what sessions
and event payloads carry), so these stamps are widened to TEXT rather
than remapped to identity's internal app_user UUID. The human-readable
prepared_by_name / prepared_by_email snapshots are unchanged.

Downgrade note: reversing to UUID only works while every stored value
parses as UUID (true for rows stamped before this fix, e.g. directly
seeded uuid test data); rows stamped with a Firebase uid will error on
downgrade — documented, acceptable for a dev-era column type fix.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("quote", "prepared_by", type_=sa.Text(), existing_nullable=True)
    op.alter_column("quote", "approved_by", type_=sa.Text(), existing_nullable=True)
    op.alter_column("quote", "issued_by", type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("quote", "issued_by", type_=sa.Uuid(), existing_nullable=True)
    op.alter_column("quote", "approved_by", type_=sa.Uuid(), existing_nullable=True)
    op.alter_column("quote", "prepared_by", type_=sa.Uuid(), existing_nullable=True)
