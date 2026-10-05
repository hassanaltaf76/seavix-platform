"""SeaVix registry service — vessel taxonomy + ports.

Run: uv run uvicorn registry.main:app --port 8003
"""
from __future__ import annotations

from fastapi import FastAPI

from .vessels import router as vessels_router


def create_app() -> FastAPI:
    app = FastAPI(title="seavix-registry")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "registry"}

    app.include_router(vessels_router)
    return app


app = create_app()
