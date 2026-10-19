"""Shared HTTP client for web → identity calls, with a test override seam.

Tests replace the transport with httpx.ASGITransport(identity_app) so both
apps run in one event loop against the same dev database (the established
pattern — Starlette TestClient and cross-loop asyncpg are both poison; see
reports 11 and 14).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .config import identity_base_url

_transport: httpx.AsyncBaseTransport | None = None


def set_identity_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


@asynccontextmanager
async def identity_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        base_url=identity_base_url(), timeout=10.0, transport=_transport
    ) as client:
        yield client


async def identity_request(
    method: str, path: str, *, json: dict | None = None,
    headers: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    async with identity_client() as c:
        return await c.request(method, path, json=json, headers=headers,
                               files=files)
