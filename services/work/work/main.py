"""SeaVix work service — RFQ → quote → job spine.

Run: uv run uvicorn work.main:app --port 8004
"""
from __future__ import annotations

from fastapi import FastAPI

from seavix_ports import InMemoryEventBus

from .spine import router as spine_router


def create_app() -> FastAPI:
    app = FastAPI(title="seavix-work")
    app.state.event_bus = InMemoryEventBus()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "work"}

    app.include_router(spine_router)
    return app


app = create_app()
