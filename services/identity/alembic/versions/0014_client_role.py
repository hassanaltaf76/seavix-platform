"""0014 RBAC catalog extension (32a): seed the "client" system role.

Revision ID: 0014
Revises: 0013

32a adds seven catalog permissions (client.view, client.create, client.edit,
quote.view, plus rfq.create/rfq.view/quote.create which already existed) and
a new seeded system role "client" holding ONLY rfq.create + rfq.view — the
registered-client role. Data-only migration: the catalog itself is code
(permissions.py, D2), and default-grant changes for owner/admin/ops are
code-derived too (invariant 10: update the derivation, not stored rows) —
system role ROWS in existing orgs keep their seeded permission arrays, so
this migration only inserts the NEW role where it does not exist yet. Fresh
databases get the same rows from seed_default_roles at registration.
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CLIENT_ROLE_PERMISSIONS = ["rfq.create", "rfq.view"]


def upgrade() -> None:
    conn = op.get_bind()
    org_ids = [r[0] for r in conn.execute(sa.text("SELECT id FROM organization"))]
    for org_id in org_ids:
        conn.execute(
            sa.text(
                "INSERT INTO role (id, org_id, name, permissions, is_system) "
                "VALUES (:id, :org, 'client', :perms, true) "
                "ON CONFLICT (org_id, name) DO NOTHING"
            ),
            {"id": str(uuid.uuid4()), "org": str(org_id),
             "perms": CLIENT_ROLE_PERMISSIONS},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM role WHERE name = 'client' AND is_system = true")
    )
