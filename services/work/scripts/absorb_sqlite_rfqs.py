"""One-shot migration: absorb the web app's temporary SQLite RFQs into work.

Source: var/web.db (task-12 shortcut table rfq: id, org_slug, vessel_imo,
survey_type, location_type, preferred_date, scope, created_at — NOTE the old
public form collected no contact_email, so contact_email stays NULL here).
Target: work.rfq with source='public', status='open', target_org_id resolved
from the org slug via the identity database (read-only lookup — this script
is the documented exception to "no cross-service DB access").

Idempotent: existing ids are skipped (ON CONFLICT DO NOTHING). Rows whose
slug no longer maps to an organization are reported and skipped.

Run from the work service directory:

    uv run python scripts/absorb_sqlite_rfqs.py [path/to/web.db]
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from work.config import database_url  # noqa: E402

PLATFORM_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SQLITE = PLATFORM_ROOT / "var" / "web.db"
IDENTITY_URL = "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity"


def read_sqlite(path: Path) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, org_slug, vessel_imo, survey_type, location_type, "
        "preferred_date, scope, created_at FROM rfq"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def resolve_org_ids(slugs: Iterable[str]) -> dict[str, str]:
    engine = create_async_engine(IDENTITY_URL)
    mapping: dict[str, str] = {}
    async with engine.connect() as conn:
        for slug in sorted(set(slugs)):
            row = (
                await conn.execute(
                    text("SELECT id FROM organization WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
            if row is not None:
                mapping[slug] = str(row[0])
    await engine.dispose()
    return mapping


async def absorb(sqlite_path: Path = DEFAULT_SQLITE) -> dict:
    rows = read_sqlite(sqlite_path) if Path(sqlite_path).exists() else []
    org_map = await resolve_org_ids(r["org_slug"] for r in rows)

    factory = async_sessionmaker(
        create_async_engine(database_url()), expire_on_commit=False
    )
    migrated, skipped = 0, []
    async with factory() as session:
        for r in rows:
            org_id = org_map.get(r["org_slug"])
            if org_id is None:
                skipped.append({"id": r["id"], "slug": r["org_slug"],
                                "reason": "unknown org slug"})
                continue
            created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc)
            res = await session.execute(
                text(
                    "INSERT INTO rfq (id, requesting_org_id, billing_org_id, "
                    "target_org_id, contact_email, vessel_imo, survey_type, "
                    "location_type, preferred_date, scope_notes, source, "
                    "status, created_at) VALUES "
                    "(:id, NULL, NULL, :target, NULL, :imo, :stype, :ltype, "
                    " :pdate, :scope, 'public', 'open', :created) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": r["id"],
                    "target": org_id,
                    "imo": r["vessel_imo"],
                    "stype": r["survey_type"],
                    "ltype": r["location_type"],
                    "pdate": date.fromisoformat(r["preferred_date"])
                    if r["preferred_date"]
                    else None,
                    "scope": r["scope"],
                    "created": created,
                },
            )
            migrated += res.rowcount if res.rowcount and res.rowcount > 0 else 0
        await session.commit()
    return {"found": len(rows), "migrated": migrated, "skipped": skipped}


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SQLITE
    result = asyncio.run(absorb(path))
    print(result)
