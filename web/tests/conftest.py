"""Web-app test wiring: run the web app and the identity service in ONE
event loop (the root `arun` loop), talking through httpx.ASGITransport.

The web app believes it calls identity over HTTP; the transport seam in
webapp.identity_client swaps the network for an in-process ASGI transport.
This keeps production behaviour identical while staying hermetic and
avoiding both TestClient and cross-loop asyncpg poisoning.
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

from app.main import app as identity_app  # noqa: E402  (identity service)

from webapp.identity_client import set_identity_transport  # noqa: E402
from webapp.main import app as web_app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _wire_identity_transport():
    set_identity_transport(httpx.ASGITransport(app=identity_app))
    yield
    set_identity_transport(None)


@pytest.fixture(scope="session")
def web_asgi():
    return web_app


@pytest.fixture(scope="session")
def id_asgi():
    return identity_app

