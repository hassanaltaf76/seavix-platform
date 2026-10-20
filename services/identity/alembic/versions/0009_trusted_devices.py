"""0009 trusted devices (task 31j — "remember this device" MFA).

Revision ID: 0009
Revises: 0008

trusted_device: the 30-day "trust this device" second-factor bypass.
The browser cookie carries an opaque token; ONLY its sha256 lives here,
bound to the user, with a 30-day expiry, a revocation timestamp and the
user-agent that enrolled it (display metadata for the account surface —
never a security input). A password reset revokes every row of the user;
logout deliberately keeps trust.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trusted_device",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_trusted_device_user", "trusted_device", ["user_id"])
    op.create_index(
        "ix_trusted_device_token_hash", "trusted_device", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_trusted_device_token_hash", table_name="trusted_device")
    op.drop_index("ix_trusted_device_user", table_name="trusted_device")
    op.drop_table("trusted_device")
