"""Organization lifecycle — creates orgs and announces them on the event bus.

Events go through the seavix_ports EventBus port (InMemoryEventBus in dev),
never through a service-specific mechanism — this is what keeps the events
rule (task 1) provable: org.created is published via the port on creation.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from seavix_ports import domain_event


async def create_organization(
    session: AsyncSession,
    bus: Any,
    name: str,
    org_type: str,
) -> dict:
    org_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO organization (id, name, type, created_at) "
            "VALUES (:id, :name, :type, now())"
        ),
        {"id": org_id, "name": name, "type": org_type},
    )
    await session.commit()

    await bus.publish(
        "org.created",
        domain_event(
            "org.created",
            "organization",
            org_id,
            {"name": name, "type": org_type},
        ),
    )
    return {"id": org_id, "name": name, "type": org_type}
