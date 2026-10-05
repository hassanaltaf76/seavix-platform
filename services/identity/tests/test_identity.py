"""Identity service tests: healthz + org.created event via the EventBus port."""
import asyncio
import os
import sys

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_healthz(arun):
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "identity"}


def test_org_created_event_published_via_bus(arun):
    """Org creation must publish a domain event through the EventBus port.

    Single event loop for the whole scenario (shared test loop, see conftest): pooled asyncpg connections
    must not be shared across event loops.
    """
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.orgs import create_organization
    from seavix_ports import InMemoryEventBus

    bus = InMemoryEventBus()
    received = []
    bus.subscribe("org.created", received.append)

    async def scenario():
        async with SessionLocal() as session:
            org = await create_organization(session, bus, "Acme", "team")
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT name, type FROM organization WHERE id = :id"
                    ),
                    {"id": org["id"]},
                )
            ).one()
        return org, row

    org, row = arun(scenario())

    # handler received exactly one envelope-shaped event
    assert len(received) == 1
    event = received[0]
    assert event["type"] == "org.created"
    assert event["aggregate"] == "organization"
    assert event["aggregate_id"] == org["id"]
    assert event["payload"] == {"name": "Acme", "type": "team"}
    assert set(event) == {
        "id",
        "type",
        "aggregate",
        "aggregate_id",
        "occurred_at",
        "payload",
    }
    # audit hook on the port recorded it too
    assert [topic for topic, _ in bus.published] == ["org.created"]
    # and the row actually landed in seavix_identity
    assert row == ("Acme", "team")
