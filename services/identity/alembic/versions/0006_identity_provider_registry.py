"""0006 identity provider registry (OIDC relying-party plumbing).

Revision ID: 0006
Revises: 0005

Org-scoped registry of external OIDC identity providers. SeaVix is the
relying party THROUGH Identity Platform's custom-OIDC-provider support:
at runtime the Admin SDK still verifies SeaVix-issued ID tokens, so this
table configures discovery, not token verification.

client_secret_ref names a SecretsProvider entry — the secret itself is
never stored here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "identity_provider",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret_ref", sa.Text(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("claims_mapping", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "display_name", name="uq_idp_org_name"),
    )
    op.create_index("ix_identity_provider_org", "identity_provider", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_identity_provider_org", table_name="identity_provider")
    op.drop_table("identity_provider")
