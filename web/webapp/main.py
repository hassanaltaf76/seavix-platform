"""SeaVix public web app — app factory.

Run: uv run uvicorn webapp.main:app --port 8000   (from web/, per Makefile dev)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from seavix_ports import AuthMiddleware, public_route

from .auth import SESSION_COOKIE, WebAuthLoader
from .pages import router as pages_router
from .auth_pages import router as auth_pages_router
from .org_pages import router as org_pages_router

WEB_ROOT = Path(__file__).resolve().parent.parent


def create_app() -> FastAPI:
    app = FastAPI(
        title="seavix-web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # deny-by-default: no unaccounted framework routes
    )
    app.add_middleware(
        AuthMiddleware,
        loader=WebAuthLoader(),
        cookie=SESSION_COOKIE,
        unauthenticated="redirect:/login",  # server-rendered UX: 303 to sign-in
        route_source=app,
    )
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")

    @app.get("/healthz")
    @public_route
    async def healthz() -> dict:
        return {"status": "ok", "service": "web"}

    app.include_router(pages_router)
    app.include_router(auth_pages_router)
    app.include_router(org_pages_router)
    return app


app = create_app()
