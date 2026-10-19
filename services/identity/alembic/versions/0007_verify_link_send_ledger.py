"""0007 verification-link send ledger purpose.

Revision ID: 0007
Revises: 0006

31e: verification-link sends (POST /api/register/verify/send) are recorded
in the email_otp table so they share the per-email 5/hour send window with
OTP codes. The purpose CHECK must admit the 'verify_link' counter purpose
(rows whose hash is a throwaway value — they are send counters, never
consumable codes; verification links confirm via Firebase oobCode).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("email_otp_purpose_check", "email_otp", type_="check")
    op.create_check_constraint(
        "email_otp_purpose_check",
        "email_otp",
        "purpose IN ('login', 'verify_email', 'verify_link')",
    )


def downgrade() -> None:
    # purge the counter rows first — they would violate the restored CHECK
    op.execute("DELETE FROM email_otp WHERE purpose = 'verify_link'")
    op.drop_constraint("email_otp_purpose_check", "email_otp", type_="check")
    op.create_check_constraint(
        "email_otp_purpose_check",
        "email_otp",
        "purpose IN ('login', 'verify_email')",
    )
