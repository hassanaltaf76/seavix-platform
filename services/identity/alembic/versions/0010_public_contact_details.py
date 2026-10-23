"""0010 public contact details (task 31m).

Revision ID: 0010
Revises: 0009

organization gains the public contact block: address (multi-line free
text, rendered as escaped paragraph blocks on the public page),
contact_email (the PUBLIC business contact — surface-separation rule:
never the account/login email), whatsapp (international format, loose
validation in the API layer). phone exists since 0002. All three are
nullable TEXT; the public Contact section renders only when at least
one of the four fields is set.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("address", sa.Text(), nullable=True))
    op.add_column(
        "organization", sa.Column("contact_email", sa.Text(), nullable=True)
    )
    op.add_column("organization", sa.Column("whatsapp", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("organization", "whatsapp")
    op.drop_column("organization", "contact_email")
    op.drop_column("organization", "address")
