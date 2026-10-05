"""Idempotent registry seed — the three vessels from real surveys + ports.

Re-runnable: vessels upsert on IMO, ports upsert on UN/LOCODE. Run from the
service directory:

    uv run python scripts/seed_registry.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from registry.db import SessionLocal  # noqa: E402

VESSELS = [
    {
        "imo": "1024948",
        "name": "RODOS",
        "cargo_class": "liquid",
        "ship_type": "oil_tanker",
        "size_class": "other",
        "dwt": None,
        "loa_m": None,
        "pump_type": "steam_turbine",
        "flag": None,
        "year_built": None,
    },
    {
        "imo": "9404572",
        "name": "FOSHAN",
        "cargo_class": "liquid",
        "ship_type": "oil_tanker",
        "size_class": "other",
        "dwt": 8761,
        "loa_m": None,
        "pump_type": "screw",
        "flag": None,
        "year_built": None,
    },
    {
        "imo": "9318620",
        "name": "GAZ DIAMOND",
        "cargo_class": "liquid",
        "ship_type": "gas_carrier",
        "size_class": "other",
        "dwt": None,
        "loa_m": None,
        "pump_type": None,
        "flag": None,
        "year_built": None,
    },
]

PORTS = [
    {"code": "AEFJR", "name": "Fujairah", "country": "AE"},
    {"code": "AEKLF", "name": "Khorfakkan", "country": "AE"},
    {"code": "ROCND", "name": "Constanta", "country": "RO"},
    {"code": "BEANR", "name": "Antwerp", "country": "BE"},
]

COLS = (
    "imo, name, cargo_class, ship_type, size_class, dwt, loa_m, "
    "pump_type, flag, year_built"
)


async def main() -> None:
    async with SessionLocal() as session:
        for v in VESSELS:
            await session.execute(
                text(
                    f"INSERT INTO vessel ({COLS}) VALUES "
                    "(:imo, :name, :cargo_class, :ship_type, :size_class, :dwt, "
                    " :loa_m, :pump_type, :flag, :year_built) "
                    "ON CONFLICT (imo) DO UPDATE SET "
                    "name = EXCLUDED.name, cargo_class = EXCLUDED.cargo_class, "
                    "ship_type = EXCLUDED.ship_type, size_class = EXCLUDED.size_class, "
                    "dwt = EXCLUDED.dwt, loa_m = EXCLUDED.loa_m, "
                    "pump_type = EXCLUDED.pump_type, flag = EXCLUDED.flag, "
                    "year_built = EXCLUDED.year_built"
                ),
                v,
            )
        for p in PORTS:
            await session.execute(
                text(
                    "INSERT INTO port (code, name, country) VALUES "
                    "(:code, :name, :country) "
                    "ON CONFLICT (code) DO UPDATE SET "
                    "name = EXCLUDED.name, country = EXCLUDED.country"
                ),
                p,
            )
        await session.commit()
    print(f"seeded {len(VESSELS)} vessels, {len(PORTS)} ports")


if __name__ == "__main__":
    asyncio.run(main())
