"""0017 org-level commercial defaults for quotations (task 32c).

Revision ID: 0017
Revises: 0016

Owner spec (task 32c): quotations prefill from ORG-level commercial
defaults, not per-quote free text. The organization table gains:

- quote_prefix TEXT NULL        — e.g. "PMX", prepended to quote numbers
- default_terms JSONB NULL      — list of text lines rendered on quotes
- payment_details JSONB NULL    — object (bank_name, branch, account_name,
                                  account_number, iban, swift)

Backfill: every pre-migration org starts from the Pak Marine default
terms (the 9 lines below, verbatim from the master doc) so existing
tenants' quotes stay byte-identical until they edit the settings.
Idempotent (only rows with default_terms IS NULL are touched);
downgrade just drops the columns — the backfill is additive data on
columns that vanish with the downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The canonical copy lives in app/profile.py (DEFAULT_TERMS) — the test
# suite asserts it against this list. Migrations stay standalone (the
# 0015 SURVEY_TYPES precedent), so the lines are mirrored here verbatim.
PAK_MARINE_DEFAULT_TERMS = [
    "Our offer is valid for 30 days from the date of this quotation.",
    "Condition report will be submitted within 36 hours of survey completion.",
    "Pre-purchase report will be submitted within 72 hours of survey completion.",
    "Payment is due within 07 days of invoice date.",
    "Travel expenses will be charged at actuals plus traveling days.",
    "Anchorage surcharge applies as applicable.",
    "Gate pass arrangements are on client's account.",
    "50% advance payment is required before mobilization.",
    "Bank charges are on client's account.",
]


def upgrade() -> None:
    op.add_column("organization", sa.Column("quote_prefix", sa.Text(), nullable=True))
    op.add_column("organization", sa.Column("default_terms", JSONB(), nullable=True))
    op.add_column("organization", sa.Column("payment_details", JSONB(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE organization SET default_terms = :terms "
            "WHERE default_terms IS NULL"
        ).bindparams(sa.bindparam("terms", type_=JSONB)),
        {"terms": PAK_MARINE_DEFAULT_TERMS},
    )


def downgrade() -> None:
    op.drop_column("organization", "payment_details")
    op.drop_column("organization", "default_terms")
    op.drop_column("organization", "quote_prefix")
