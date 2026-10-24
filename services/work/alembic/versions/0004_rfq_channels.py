"""0004 RFQ two-channel intake + client provenance (task 32b).

Revision ID: 0004
Revises: 0003

Field spec: the Pak Marine quote documents (master doc §4) — an RFQ
carries the client's reference number and the vessel name, and every RFQ
records WHICH channel it arrived on:

- rfq.reference_no TEXT NULL  — the client's own reference (Pak Marine
  quotes show "Reference: Pre Purchase inspection at Karachi, …").
- rfq.vessel_name TEXT NULL   — the quoting surveyor addresses the vessel,
  not just its IMO ("Condition Assessment Survey of Barge …").
- rfq.port_name TEXT NULL     — free-text port/place alongside
  location_type (the RFQ form collects it; §2/§3 of the spec render it in
  the list's Location column and the detail page).
- rfq.channel TEXT NOT NULL DEFAULT 'public_page'
  CHECK (public_page | manual_whatsapp | manual_email) — public_page is
  the marketing funnel; manual_* is a tenant raising a WhatsApp/email
  request "for record". Data migration maps the old source column:
  'public' -> 'public_page', 'internal' -> 'manual_email' (the old
  tenant-raised intake; WhatsApp-vs-email history is not recoverable).
- rfq.client_id UUID NULL — the client_directory EDGE for manual-channel
  RFQs (tenant's directory entry the client was linked to). NO FK: the
  directory lives in this database but the edge may outlive… rather, the
  same no-cross-DB-FK rule does not even apply here — the point is the
  edge is soft reference data, deleted/re-created freely; keep it
  unenforced like every other org reference in the spine.

Status vocabulary: 'open' is renamed to 'awaiting_quote' (the display
label is "awaiting quote" — the tenant list shows fresh manual intake as
awaiting-quote before a quote goes out). The CHECK constraint is swapped
to match; existing rows are migrated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHANNELS = ("public_page", "manual_whatsapp", "manual_email")


def upgrade() -> None:
    op.add_column("rfq", sa.Column("reference_no", sa.Text(), nullable=True))
    op.add_column("rfq", sa.Column("vessel_name", sa.Text(), nullable=True))
    op.add_column("rfq", sa.Column("port_name", sa.Text(), nullable=True))
    op.add_column("rfq", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.add_column(
        "rfq",
        sa.Column(
            "channel",
            sa.Text(),
            nullable=False,
            server_default="public_page",
        ),
    )
    # Drop the old status CHECK, migrate the rows, then land the new
    # vocabulary — the old CHECK would reject 'awaiting_quote' and the
    # new one rejects 'open', so the data move must happen between them.
    op.drop_constraint("ck_rfq_status", "rfq", type_="check")
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE rfq SET channel = CASE source "
            "WHEN 'internal' THEN 'manual_email' ELSE 'public_page' END"
        )
    )
    conn.execute(
        sa.text("UPDATE rfq SET status = 'awaiting_quote' WHERE status = 'open'")
    )
    op.create_check_constraint(
        "ck_rfq_status", "rfq",
        sa.text("status IN ('awaiting_quote', 'quoted', 'accepted', 'declined', "
                "'expired')"),
    )
    op.create_check_constraint(
        "ck_rfq_channel", "rfq",
        sa.text("channel IN ('public_page', 'manual_whatsapp', 'manual_email')"),
    )
    # 32b: 'manual' joins the legacy source column's vocabulary — source
    # is superseded by channel but keeps recording the intake family for
    # old readers (public funnel / tenant spine / manual for-record).
    op.drop_constraint("ck_rfq_source", "rfq", type_="check")
    op.create_check_constraint(
        "ck_rfq_source", "rfq",
        sa.text("source IN ('public', 'internal', 'manual')"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE rfq SET source = 'internal' WHERE source = 'manual'")
    )
    op.drop_constraint("ck_rfq_source", "rfq", type_="check")
    op.create_check_constraint(
        "ck_rfq_source", "rfq", sa.text("source IN ('public', 'internal')")
    )
    op.drop_constraint("ck_rfq_status", "rfq", type_="check")
    op.create_check_constraint(
        "ck_rfq_status", "rfq",
        sa.text("status IN ('open', 'quoted', 'accepted', 'declined', 'expired')"),
    )
    op.drop_constraint("ck_rfq_channel", "rfq", type_="check")
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE rfq SET status = 'open' WHERE status = 'awaiting_quote'")
    )
    op.drop_column("rfq", "channel")
    op.drop_column("rfq", "client_id")
    op.drop_column("rfq", "port_name")
    op.drop_column("rfq", "vessel_name")
    op.drop_column("rfq", "reference_no")
