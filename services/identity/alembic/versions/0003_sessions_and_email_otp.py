"""0003 sessions and email OTP (D1 second factor).

Revision ID: 0003
Revises: 0002

Adds the server-side pieces of the email-OTP auth design:
- app_user.email_verified — registration email verification gate
- auth_session — opaque server-side sessions (the web app's HttpOnly cookie
  carries only the session id; revocation and member deactivation take
  effect immediately, which a stateless JWT cookie could not do)
- email_otp — single-use 6-digit codes, hashed at rest, with per-email
  send window and per-code attempt counter enforced in the service layer
  (D1: 5 sends/hour/email, 5 attempts/code, 10-minute TTL).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("firebase_uid", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_auth_session_user", "auth_session", ["user_id"])
    op.create_index("ix_auth_session_expires", "auth_session", ["expires_at"])

    op.create_table(
        "email_otp",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "purpose",
            sa.Text(),
            sa.CheckConstraint("purpose IN ('login', 'verify_email')"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_email_otp_email_created", "email_otp", ["email", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_email_otp_email_created", table_name="email_otp")
    op.drop_table("email_otp")
    op.drop_index("ix_auth_session_expires", table_name="auth_session")
    op.drop_index("ix_auth_session_user", table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_column("app_user", "email_verified")
