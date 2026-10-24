"""0005 quote document (task 32c).

Revision ID: 0005
Revises: 0004

The Pak Marine quote-document field spec (master doc §4) turns the bare
quote row into a printable document:

- quote_sequence (tenant_org_id, year) -> last_seq: the per-tenant,
  per-year quote number allocator. One atomic upsert (ON CONFLICT
  DO UPDATE ... + 1 RETURNING) hands out the next sequence — concurrent
  creates serialize on the row instead of colliding on the number.
- quote.tenant_org_id: the quoting (target) org — the authorization
  boundary for every quote read/write (org-scoping IS authz, task-23).
- quote.quote_no TEXT NOT NULL + UNIQUE(tenant_org_id, quote_no): the
  human-facing number, "PREFIX-NN-YEAR" (Pak Marine style).
- quote.quote_date DATE NOT NULL DEFAULT CURRENT_DATE: the document date
  (drives the numbering year).
- Echo + snapshot block: reference_no/vessel_name/vessel_imo/survey_types/
  location_type/port_name/scope_notes are ECHOED from the RFQ at creation;
  client_* fields are a SNAPSHOT of the client org (directory edge ->
  identity org, or the requesting org for public intake) so later edits in
  the directory never rewrite issued documents; prepared_by(_name/_email)
  stamps the author; terms/bank_details default from the org profile but
  are stored per-quote so each document is self-contained; approved_by/
  approved_at/issued_by/issued_at stamp the human approve+issue gate.
- quote.updated_at TIMESTAMPTZ NOT NULL DEFAULT now().

Backfill note (documented deviation): the spec'd quote_no PREFIX is the
tenant's org SLUG normalized — but slugs live in IDENTITY's database and
work never reads another service's DB (not even from a migration). The
work DB has no slug to normalize, so the backfill applies the same
normalization (uppercase, alphanumerics kept, '-' runs, truncate 12) to
the tenant org id's hex form, e.g. "1E150F635E12-01-2026". Rows created
after this migration get the slug-derived prefix at runtime via the
identity org lookup. quote_sequence rows are upserted so last_seq equals
the highest number assigned per (tenant, year).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quote_sequence",
        sa.Column("tenant_org_id", sa.Uuid(), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
    )

    # Nullable first — the table has live rows, so NOT NULL lands only
    # after the backfill (same 3-step dance as any populated ALTER).
    op.add_column("quote", sa.Column("tenant_org_id", sa.Uuid(), nullable=True))
    op.add_column("quote", sa.Column("quote_no", sa.Text(), nullable=True))
    op.add_column(
        "quote",
        sa.Column("quote_date", sa.Date(), nullable=False,
                  server_default=sa.text("CURRENT_DATE")),
    )
    op.add_column(
        "quote",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.add_column("quote", sa.Column("reference_no", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("vessel_name", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("vessel_imo", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("survey_types", JSONB(), nullable=True))
    op.add_column("quote", sa.Column("location_type", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("port_name", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("scope_notes", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("client_name", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("client_contact_person", sa.Text(),
                                     nullable=True))
    op.add_column("quote", sa.Column("client_designation", sa.Text(),
                                     nullable=True))
    op.add_column("quote", sa.Column("client_address", JSONB(), nullable=True))
    op.add_column("quote", sa.Column("client_country", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("client_phone", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("client_email", sa.Text(), nullable=True))
    op.add_column("quote", sa.Column("prepared_by", sa.Uuid(), nullable=True))
    op.add_column("quote", sa.Column("prepared_by_name", sa.Text(),
                                     nullable=True))
    op.add_column("quote", sa.Column("prepared_by_email", sa.Text(),
                                     nullable=True))
    op.add_column("quote", sa.Column("terms", JSONB(), nullable=True))
    op.add_column("quote", sa.Column("bank_details", JSONB(), nullable=True))
    op.add_column("quote", sa.Column("approved_by", sa.Uuid(), nullable=True))
    op.add_column("quote", sa.Column("approved_at", sa.DateTime(timezone=True),
                                     nullable=True))
    op.add_column("quote", sa.Column("issued_by", sa.Uuid(), nullable=True))
    op.add_column("quote", sa.Column("issued_at", sa.DateTime(timezone=True),
                                     nullable=True))

    # --- backfill ---
    # tenant comes from the RFQ the quote belongs to (the FK guarantees a
    # parent row, so every quote is covered).
    op.execute(
        "UPDATE quote SET tenant_org_id = rfq.target_org_id "
        "FROM rfq WHERE quote.rfq_id = rfq.id"
    )
    # document date = the day the quote was created.
    op.execute("UPDATE quote SET quote_date = created_at::date")
    # Per-tenant/year numbering walk (ordered by creation so the oldest
    # row gets -01-), then the sequence rows that match what was assigned.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DO $$
            DECLARE
                r RECORD;
                v_seq INT;
                v_prefix TEXT;
            BEGIN
                FOR r IN
                    SELECT q.id AS qid, rfq.target_org_id AS tenant,
                           extract(year FROM q.created_at)::int AS yr
                    FROM quote q
                    JOIN rfq ON rfq.id = q.rfq_id
                    ORDER BY rfq.target_org_id,
                             extract(year FROM q.created_at),
                             q.created_at
                LOOP
                    -- slug lives in identity's DB (unreadable from here);
                    -- the tenant uuid's hex form normalizes identically.
                    v_prefix := upper(
                        left(replace(r.tenant::text, '-', ''), 12));
                    UPDATE quote_sequence
                       SET last_seq = last_seq + 1
                     WHERE tenant_org_id = r.tenant AND year = r.yr
                    RETURNING last_seq INTO v_seq;
                    IF v_seq IS NULL THEN
                        v_seq := 1;
                        INSERT INTO quote_sequence
                            (tenant_org_id, year, last_seq)
                        VALUES (r.tenant, r.yr, 1);
                    END IF;
                    UPDATE quote
                       SET quote_no = v_prefix || '-' ||
                                      lpad(v_seq::text, 2, '0') || '-' || r.yr
                     WHERE id = r.qid;
                END LOOP;
            END $$;
            """
        )
    )

    op.alter_column("quote", "tenant_org_id", nullable=False)
    op.alter_column("quote", "quote_no", nullable=False)
    op.create_unique_constraint(
        "uq_quote_tenant_no", "quote", ["tenant_org_id", "quote_no"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_quote_tenant_no", "quote", type_="unique")
    op.drop_column("quote", "issued_at")
    op.drop_column("quote", "issued_by")
    op.drop_column("quote", "approved_at")
    op.drop_column("quote", "approved_by")
    op.drop_column("quote", "bank_details")
    op.drop_column("quote", "terms")
    op.drop_column("quote", "prepared_by_email")
    op.drop_column("quote", "prepared_by_name")
    op.drop_column("quote", "prepared_by")
    op.drop_column("quote", "client_email")
    op.drop_column("quote", "client_phone")
    op.drop_column("quote", "client_country")
    op.drop_column("quote", "client_address")
    op.drop_column("quote", "client_designation")
    op.drop_column("quote", "client_contact_person")
    op.drop_column("quote", "client_name")
    op.drop_column("quote", "scope_notes")
    op.drop_column("quote", "port_name")
    op.drop_column("quote", "location_type")
    op.drop_column("quote", "survey_types")
    op.drop_column("quote", "vessel_imo")
    op.drop_column("quote", "vessel_name")
    op.drop_column("quote", "reference_no")
    op.drop_column("quote", "updated_at")
    op.drop_column("quote", "quote_date")
    op.drop_column("quote", "quote_no")
    op.drop_column("quote", "tenant_org_id")
    op.drop_table("quote_sequence")
