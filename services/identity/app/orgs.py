"""Organization lifecycle — creates orgs and announces them on the event bus.

Events go through the seavix_ports EventBus port (InMemoryEventBus in dev),
never through a service-specific mechanism — this is what keeps the events
rule (task 1) provable: org.created is published via the port on creation.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from seavix_ports import domain_event


def _generate_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def create_organization(
    session: AsyncSession,
    bus: Any,
    name: str,
    org_type: str,
    slug: str | None = None,
) -> dict:
    org_id = str(uuid.uuid4())
    slug = slug or _generate_slug(name)
    await session.execute(
        text(
            "INSERT INTO organization (id, name, type, slug, created_at) "
            "VALUES (:id, :name, :type, :slug, now())"
        ),
        {"id": org_id, "name": name, "type": org_type, "slug": slug},
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
    return {"id": org_id, "name": name, "type": org_type, "slug": slug}
