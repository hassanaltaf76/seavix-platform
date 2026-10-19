"""Web-app test wiring: run the web app, the identity service and the
registry in ONE event loop (the root `arun` loop), talking through
httpx.ASGITransport seams.

The web app believes it calls identity/registry over HTTP; the transport
seams in webapp.identity_client / webapp.registry_client swap the network
for in-process ASGI transports. This keeps production behaviour identical
while staying hermetic and avoiding both TestClient and cross-loop asyncpg
poisoning. The registry tests share the same loop (their private conftest
was removed in 31f — one engine, one loop, per reports 06/11/14).
"""
import os
import sys

import httpx
import pytest

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "services", "identity")
    ),
)
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "services", "registry")
    ),
)

from app.main import app as identity_app  # noqa: E402  (identity service)
from registry.main import app as registry_app  # noqa: E402  (registry service)

from webapp.identity_client import set_identity_transport  # noqa: E402
from webapp.main import app as web_app  # noqa: E402
from webapp.registry_client import set_registry_transport  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _wire_service_transports():
    set_identity_transport(httpx.ASGITransport(app=identity_app))
    set_registry_transport(httpx.ASGITransport(app=registry_app))
    yield
    set_identity_transport(None)
    set_registry_transport(None)


@pytest.fixture(scope="session")
def web_asgi():
    return web_app


@pytest.fixture(scope="session")
def id_asgi():
    return identity_app

