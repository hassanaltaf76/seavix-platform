"""Absorption test: fixture SQLite → work.rfq with org mapping via identity DB."""
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault(
    "WORK_DATABASE_URL", "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_work"
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from work.db import SessionLocal  # noqa: E402


def _make_sqlite(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE rfq (id TEXT PRIMARY KEY, org_slug TEXT NOT NULL, "
        "vessel_imo TEXT NOT NULL, survey_type TEXT NOT NULL, "
        "location_type TEXT NOT NULL, preferred_date TEXT, scope TEXT, "
        "created_at REAL NOT NULL)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO rfq VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["org_slug"], r["vessel_imo"], r["survey_type"],
             r["location_type"], r.get("preferred_date"), r.get("scope"),
             r.get("created_at", time.time())),
        )
    conn.commit()
    conn.close()
    return path


def test_absorb_fixture_sqlite(arun, tmp_path):
    from scripts.absorb_sqlite_rfqs import absorb

    # a slug that exists in the dev identity DB (seeded in task 11) and one
    # that does not (must be skipped, not crash)
    rows = [
        {"id": str(uuid.uuid4()), "org_slug": "bluewater-surveys",
         "vessel_imo": "1024948", "survey_type": "condition",
         "location_type": "anchorage", "scope": "Old intake row",
         "created_at": 1759000000.0},
        {"id": str(uuid.uuid4()), "org_slug": "ghost-co",
         "vessel_imo": "9404572", "survey_type": "bunker",
         "location_type": "terminal"},
    ]
    sqlite_path = _make_sqlite(tmp_path / "fixture.db", rows)

    result = arun(absorb(sqlite_path))
    assert result["found"] == 2
    assert result["migrated"] == 1
    assert result["skipped"] == [{"id": rows[1]["id"], "slug": "ghost-co",
                                  "reason": "unknown org slug"}]

    async def check():
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text("SELECT target_org_id, source, status, scope_notes, "
                         "contact_email, created_at FROM rfq WHERE id = :id"),
                    {"id": rows[0]["id"]},
                )
            ).mappings().one()
        return row

    row = arun(check())
    assert row["source"] == "public" and row["status"] == "open"
    assert row["scope_notes"] == "Old intake row"
    assert row["contact_email"] is None  # old form collected no email
    assert str(row["created_at"].tzinfo)  # timestamptz preserved
    # target maps to bluewater-surveys' real org id in identity
    async def org_id():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity"
        )
        async with engine.connect() as conn:
            r = await conn.execute(
                text("SELECT id FROM organization WHERE slug = 'bluewater-surveys'")
            )
            return str(r.scalar_one())

    assert str(row["target_org_id"]) == arun(org_id())

    # idempotent: second run migrates nothing
    again = arun(absorb(sqlite_path))
    assert again["migrated"] == 0 and again["skipped"] == result["skipped"]
