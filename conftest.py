"""Root pytest wiring for the whole workspace.

One shared event loop for every async test in the process. Service engines
create pooled asyncpg connections bound to the loop that first uses them;
per-test or per-package loops therefore poison each other across packages
(see reports 06/11/14). A single root loop is the durable fix.

Local-emulator env defaults live here too, set before any service module is
imported (first-import-wins env semantics).
"""
import asyncio
import os

os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
os.environ.setdefault("FIREBASE_PROJECT_ID", "seavix-dev")
os.environ.setdefault("SEAVIX_DEV_OTP_ENDPOINT", "1")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity",
)

import pytest

_loop = asyncio.new_event_loop()


@pytest.fixture()
def arun():
    return _loop.run_until_complete


@pytest.fixture(scope="session", autouse=True)
def _close_loop_at_end():
    yield
    pending = asyncio.all_tasks(_loop)
    for t in pending:
        t.cancel()
    if pending:
        _loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    _loop.close()
