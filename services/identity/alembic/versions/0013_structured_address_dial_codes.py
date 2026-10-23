"""0013 structured address + country dial codes (task 31t).

Revision ID: 0013
Revises: 0012

Owner feedback (23 Oct): "address should be normal address fields not one
free text, with country; phone and WhatsApp need a prepopulated
country-code field". The organization table gains the structured public
contact fields:

- address_line1 TEXT NULL (address_line2 / city / state / postal_code
  TEXT NULL alongside it; all optional, empties are skipped at render)
- country_code TEXT NULL — ISO-3166 alpha-2, validated at the API layer
- phone_cc / whatsapp_cc TEXT NULL — international dial code (e.g.
  "+971"); phone / whatsapp keep holding the NATIONAL number

LEGACY: the 0010 free-text `address` column STAYS but is DEPRECATED —
when any structured address field is set, structured wins and the legacy
text is not rendered (rendering rule, documented in the 31t report);
legacy-only profiles keep rendering as before (back-compat).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country_code",
    "phone_cc",
    "whatsapp_cc",
)


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("organization", sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    for column in reversed(COLUMNS):
        op.drop_column("organization", column)
