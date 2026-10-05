"""SeaVix public web app — app factory.

Run: uv run uvicorn webapp.main:app --port 8000   (from web/, per Makefile dev)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .pages import router as pages_router

WEB_ROOT = Path(__file__).resolve().parent.parent


def create_app() -> FastAPI:
    app = FastAPI(title="seavix-web")
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "web"}

    app.include_router(pages_router)
    return app


app = create_app()
