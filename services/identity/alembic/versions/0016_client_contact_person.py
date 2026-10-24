"""0016 client contact person + designation (task 32b, amendment to 32a).

Revision ID: 0016
Revises: 0015

Field spec: the Pak Marine quote documents (master doc §4) — a quote
addresses a person ("Dear Capt. Amin Dar") with a designation
("Managing Director"). Client organizations therefore gain:

- contact_person TEXT NULL
- designation TEXT NULL

Both live on the shared organization table (they are client-directory
fields; NULL for every other org type). Same shape as the 31t contact
block: optional, free text, validated at the API boundary.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("contact_person", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("designation", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("organization", "designation")
    op.drop_column("organization", "contact_person")
