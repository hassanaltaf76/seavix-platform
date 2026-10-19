"""Shared HTTP client for web → registry calls, with a test override seam.

Mirrors identity_client: tests replace the transport with
httpx.ASGITransport(registry_app) so both apps run in one event loop
against the same dev database. The registry owns the reference data
(UN/LOCODE ports, vessel taxonomy); the web app only reads it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .config import registry_base_url

_transport: httpx.AsyncBaseTransport | None = None


def set_registry_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


@asynccontextmanager
async def registry_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        base_url=registry_base_url(), timeout=10.0, transport=_transport
    ) as client:
        yield client


async def registry_request(
    method: str, path: str, *, params: dict | None = None,
) -> httpx.Response:
    async with registry_client() as c:
        return await c.request(method, path, params=params)


async def resolve_ports(codes: list[str]) -> list[dict]:
    """UN/LOCODE codes -> [{code, name, country}] in request order via the
    registry bulk lookup. Empty input short-circuits (no registry round
    trip). Unknown codes are dropped by the registry."""
    if not codes:
        return []
    resp = await registry_request(
        "GET", "/ports/lookup", params={"codes": ",".join(codes)}
    )
    if resp.status_code != 200:
        return []
    return resp.json()["ports"]
