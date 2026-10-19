"""One-time UN/LOCODE ports import (task 31f) — idempotent, re-runnable.

Source: the UN/LOCODE code list published by UNECE (United Nations
Economic Commission for Europe), Code for Trade and Transport Locations.
The canonical distribution page is

    https://unece.org/trade/cefact/unlocode-text-file

but unece.org sits behind bot protection and answers non-browser agents
with 403 (verified 2026-10-19). This script therefore fetches the public
UNECE-derived mirror maintained by the Open Knowledge Foundation datahub
(datasets/un-locode), which republishes the UNECE table verbatim:

    https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv

Import date: 2026-10-19. Re-run any time to refresh: ports upsert on
UN/LOCODE (code), so the operation is idempotent and never duplicates.

Scope: rows whose Function classifier contains "1" (sea port / harbour /
marina per the UN/LOCODE function digits), withdrawn rows (Change=X)
excluded. Name = NameWoDiacritics (search-friendly) with Name fallback.

Run from the service directory:

    uv run python scripts/import_unlocode_ports.py            # full ports import
    uv run python scripts/import_unlocode_ports.py --limit 300  # top-N fallback
    uv run python scripts/import_unlocode_ports.py --dry-run    # count only
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from registry.db import SessionLocal  # noqa: E402

DEFAULT_URL = (
    "https://raw.githubusercontent.com/datasets/un-locode/"
    "main/data/code-list.csv"
)
# UN/LOCODE function digits: 1 = port (used as sea/river harbour in this
# dataset). Positions 2-7 cover rail/road/airport/postal/multimodal/etc.
PORT_FUNCTION = "1"
CHUNK = 500


def parse_ports(body: str) -> list[dict]:
    """CSV -> [{'code','name','country'}] for port-function rows."""
    rows = csv.DictReader(io.StringIO(body))
    ports: dict[str, dict] = {}
    for row in rows:
        if row.get("Change") == "X":  # withdrawn from the code list
            continue
        function = row.get("Function") or ""
        if PORT_FUNCTION not in function:
            continue
        country = (row.get("Country") or "").strip().upper()
        location = (row.get("Location") or "").strip().upper()
        if not country or not location:
            continue
        name = (row.get("NameWoDiacritics") or "").strip() or \
               (row.get("Name") or "").strip()
        if not name:
            continue
        ports.setdefault(country + location,
                         {"code": country + location, "name": name,
                          "country": country})
    return sorted(ports.values(), key=lambda p: p["code"])


async def import_ports(ports: list[dict]) -> int:
    async with SessionLocal() as session:
        for i in range(0, len(ports), CHUNK):
            chunk = ports[i:i + CHUNK]
            await session.execute(
                text(
                    "INSERT INTO port (code, name, country) VALUES "
                    "(:code, :name, :country) "
                    "ON CONFLICT (code) DO UPDATE SET "
                    "name = EXCLUDED.name, country = EXCLUDED.country"
                ),
                chunk,
            )
        await session.commit()
    return len(ports)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--limit", type=int, default=None,
                    help="import at most N ports (fallback; the full port "
                         "subset is only a few thousand rows)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fetched_at = datetime.now(timezone.utc).isoformat()
    resp = httpx.get(args.url, follow_redirects=True, timeout=60.0)
    resp.raise_for_status()
    ports = parse_ports(resp.text)
    if args.limit is not None:
        ports = ports[: args.limit]

    print(f"source      : {args.url}")
    print(f"fetched at  : {fetched_at}")
    print(f"port rows   : {len(ports)}")
    if args.dry_run:
        print("dry-run: nothing written")
        return

    n = asyncio.run(import_ports(ports))
    print(f"imported    : {n} ports (upsert on code, idempotent)")


if __name__ == "__main__":
    main()
