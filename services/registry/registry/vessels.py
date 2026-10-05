"""Vessel/port API: reads + IMO-keyed upsert."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from .db import SessionLocal

router = APIRouter()

IMO_RE = re.compile(r"^[0-9]{7}$")
CARGO_CLASSES = ("liquid", "dry")
SHIP_TYPES = (
    "oil_tanker", "chemical_tanker", "gas_carrier", "bulk_carrier",
    "general_cargo", "container", "passenger", "offshore_supply", "tug",
)
SIZE_CLASSES = (
    "handysize", "handamax", "panamax", "aframax", "suezmax", "capesize", "other",
)
PUMP_TYPES = ("steam_turbine", "framo", "screw", "none")


class VesselIn(BaseModel):
    imo: str
    name: str = Field(min_length=1, max_length=200)
    cargo_class: Literal[*CARGO_CLASSES]
    ship_type: Literal[*SHIP_TYPES]
    size_class: Literal[*SIZE_CLASSES] = "other"
    dwt: float | None = None
    loa_m: float | None = None
    pump_type: Literal[*PUMP_TYPES] | None = None
    flag: str | None = None
    year_built: int | None = None

    @field_validator("imo")
    @classmethod
    def imo_format(cls, v: str) -> str:
        if not IMO_RE.fullmatch(v):
            raise ValueError("imo must be exactly 7 digits")
        return v


class VesselOut(VesselIn):
    model_config = {"extra": "forbid"}


INSERT_COLS = (
    "imo, name, cargo_class, ship_type, size_class, dwt, loa_m, "
    "pump_type, flag, year_built"
)
SELECT_COLS = (
    "imo, name, cargo_class, ship_type, size_class, "
    "dwt::float8 AS dwt, loa_m::float8 AS loa_m, "
    "pump_type, flag, year_built"
)


@router.get("/vessels")
async def search_vessels(
    search: str = Query("", max_length=100),
) -> dict:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {SELECT_COLS} FROM vessel "
                    "WHERE name ILIKE :frag ORDER BY name LIMIT 50"
                ),
                {"frag": f"%{search}%"},
            )
        ).mappings().all()
    return {"vessels": [dict(r) for r in rows]}


@router.get("/vessels/{imo}")
async def get_vessel(imo: str) -> dict:
    if not IMO_RE.fullmatch(imo):
        raise HTTPException(status_code=422, detail="imo must be exactly 7 digits")
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(f"SELECT {SELECT_COLS} FROM vessel WHERE imo = :imo"),
                {"imo": imo},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="vessel not found")
    return dict(row)


@router.post("/vessels")
async def upsert_vessel(payload: VesselIn) -> dict:
    """Upsert keyed by IMO: existing row is overwritten, never duplicated."""
    data = payload.model_dump()
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT 1 FROM vessel WHERE imo = :imo"), {"imo": data["imo"]}
            )
        ).first()
        await session.execute(
            text(
                f"INSERT INTO vessel ({INSERT_COLS}) VALUES "
                "(:imo, :name, :cargo_class, :ship_type, :size_class, :dwt, "
                " :loa_m, :pump_type, :flag, :year_built) "
                "ON CONFLICT (imo) DO UPDATE SET "
                "name = EXCLUDED.name, cargo_class = EXCLUDED.cargo_class, "
                "ship_type = EXCLUDED.ship_type, size_class = EXCLUDED.size_class, "
                "dwt = EXCLUDED.dwt, loa_m = EXCLUDED.loa_m, "
                "pump_type = EXCLUDED.pump_type, flag = EXCLUDED.flag, "
                "year_built = EXCLUDED.year_built"
            ),
            data,
        )
        await session.commit()
    return {"imo": data["imo"], "result": "updated" if existing else "created"}


@router.get("/ports")
async def search_ports(
    search: str = Query("", max_length=100),
) -> dict:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT code, name, country FROM port "
                    "WHERE name ILIKE :frag OR code ILIKE :frag "
                    "ORDER BY name LIMIT 50"
                ),
                {"frag": f"%{search}%"},
            )
        ).mappings().all()
    return {"ports": [dict(r) for r in rows]}
