"""SeaVix identity service — app factory.

Run (per Makefile): uv run uvicorn app.main:app --port $PORT --reload
"""
from __future__ import annotations

from fastapi import FastAPI

from seavix_ports import InMemoryEventBus

from . import public, register
from .orgs import create_organization


def create_app() -> FastAPI:
    app = FastAPI(title="seavix-identity")
    app.state.event_bus = InMemoryEventBus()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "identity"}

    app.include_router(register.router)
    app.include_router(public.router)
    return app


app = create_app()
