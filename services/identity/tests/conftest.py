"""Shared event loop for all async tests in this package.

The service modules keep a process-wide async engine whose pooled asyncpg
connections are bound to the loop that first used them. asyncio.run() per
test creates a new loop each time; the second test then inherits pooled
connections bound to a dead loop and dies with
"cannot rollback; the transaction is in error state".
One shared loop for the whole pytest process fixes it (see reports/06 and
/11 for the full diagnosis).
"""
import asyncio

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
