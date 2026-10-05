"""AuthzClient integration tests against the local OpenFGA store.

Requires the dev infra (make infra) and the dev store from task 05, whose
tuples still exist: user:alice full grant:g-1 (+ organization:org-1
parent_org grant:g-1). These re-run task 05's checks through the port.
"""
import asyncio
import os
import uuid

import pytest

# Dev-store integration test: point at the local emulators explicitly.
os.environ.setdefault("OPENFGA_HTTP_ADDR", "localhost:18080")
os.environ.setdefault("OPENFGA_STORE_ID", "01M45WT1F2VSVX6SKWGRTY17BB")

from seavix_ports import AuthzClient, AuthzTuple  # noqa: E402


@pytest.fixture(scope="module")
def client() -> AuthzClient:
    return AuthzClient.from_env()


def test_task05_checks_through_port(client):
    async def scenario():
        return (
            await client.check("user:alice", "full", "grant:g-1"),
            await client.check("user:bob", "full", "grant:g-1"),
            await client.check("user:alice", "readonly", "grant:g-1"),
        )

    alice_full, bob_full, alice_readonly = asyncio.run(scenario())
    assert alice_full is True
    assert bob_full is False
    assert alice_readonly is False


def test_write_then_delete_tuple_roundtrip(client):
    suffix = uuid.uuid4().hex[:8]
    obj = f"grant:rt-{suffix}"
    t = AuthzTuple(user="user:roundtrip", relation="readonly", object=obj)

    async def scenario():
        await client.write_tuples([t])
        granted = await client.check("user:roundtrip", "readonly", obj)
        await client.delete_tuples([t])
        revoked = await client.check("user:roundtrip", "readonly", obj)
        return granted, revoked

    granted, revoked = asyncio.run(scenario())
    assert granted is True
    assert revoked is False
