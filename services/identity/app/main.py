"""SeaVix identity service — app factory.

Run (per Makefile): uv run uvicorn app.main:app --port $PORT --reload
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from seavix_ports import AuthMiddleware, InMemoryEventBus, public_route

from . import authn, internal, members, profile, public, rbac, register
from .orgs import create_organization
from .security import IdentityAuthLoader

logging.basicConfig(level=logging.INFO)  # route app INFO logs (DEV-OTP etc.) to console


def create_app() -> FastAPI:
    app = FastAPI(
        title="seavix-identity",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # deny-by-default: no unaccounted framework routes
    )
    app.state.event_bus = InMemoryEventBus()
    app.add_middleware(
        AuthMiddleware,
        loader=IdentityAuthLoader(),
        unauthenticated="json",  # API service: 401 JSON
        route_source=app,
    )

    @app.get("/healthz")
    @public_route
    async def healthz() -> dict:
        return {"status": "ok", "service": "identity"}

    app.include_router(register.router)
    app.include_router(authn.router)
    app.include_router(rbac.router)
    app.include_router(members.router)
    app.include_router(public.router)
    app.include_router(profile.router)
    app.include_router(internal.router)
    return app


app = create_app()
