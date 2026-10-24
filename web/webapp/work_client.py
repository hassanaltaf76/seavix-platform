"""Shared HTTP client for web → work-service calls, with a test override
seam — the same pattern as identity_client.py: tests swap the transport
for an in-process ASGI transport so both apps run in one event loop.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .config import work_base_url

_transport: httpx.AsyncBaseTransport | None = None


def set_work_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


@asynccontextmanager
async def work_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        base_url=work_base_url(), timeout=10.0, transport=_transport
    ) as client:
        yield client


async def work_request(
    method: str, path: str, *, json: dict | None = None,
    headers: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    async with work_client() as c:
        return await c.request(method, path, json=json, headers=headers,
                               params=params)
