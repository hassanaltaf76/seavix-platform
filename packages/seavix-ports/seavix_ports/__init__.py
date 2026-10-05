"""SeaVix ports — the interfaces that keep GCP out of service code.

Rule: services import ONLY from this package for cloud capabilities.
Dev wires the local implementations; prod wires the GCP implementations
(same class names, different module: seavix_ports_gcp). No service file
may import google.cloud.* directly.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


# ---------------------------------------------------------------- events

class EventBus(Protocol):
    async def publish(self, topic: str, event: dict) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[dict], Any]) -> None: ...


@dataclass
class InMemoryEventBus:
    """Dev implementation. Prod: PubSubBus publishing to Pub/Sub topics."""

    published: list[tuple[str, dict]] = field(default_factory=list)
    _handlers: dict[str, list[Callable]] = field(default_factory=dict)

    def subscribe(self, topic: str, handler: Callable[[dict], Any]) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    async def publish(self, topic: str, event: dict) -> None:
        self.published.append((topic, event))            # test hook / audit
        for h in self._handlers.get(topic, []):
            h(event)


def domain_event(event_type: str, aggregate: str, aggregate_id: str,
                 payload: dict) -> dict:
    """Standard envelope — same shape dev and prod; BigQuery consumes it later."""
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "aggregate": aggregate,
        "aggregate_id": aggregate_id,
        "occurred_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "payload": payload,
    }


# ---------------------------------------------------------------- blobs

class BlobStore(Protocol):
    async def presign_upload(self, key: str, ttl_seconds: int = 900) -> str: ...
    async def presign_download(self, key: str, ttl_seconds: int = 900) -> str: ...


@dataclass
class LocalBlobStore:
    """Dev implementation: files under ./var/blobs, URLs are local paths."""
    root: Path = Path("./var/blobs")

    async def presign_upload(self, key: str, ttl_seconds: int = 900) -> str:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        return f"local://{dest}"

    async def presign_download(self, key: str, ttl_seconds: int = 900) -> str:
        return f"local://{self.root / key}"


# ---------------------------------------------------------------- auth

@dataclass(frozen=True)
class AuthenticatedUser:
    uid: str
    email: str
    org_memberships: tuple[str, ...] = ()   # org_ids; identity service fills from DB


class AuthProvider(Protocol):
    async def verify_token(self, id_token: str) -> AuthenticatedUser: ...


@dataclass
class FirebaseAuthProvider:
    """Works against the emulator in dev (FIREBASE_AUTH_EMULATOR_HOST=localhost:9099)
    and Identity Platform in prod — same code, env-var difference only."""

    project_id: str = "seavix-dev"

    async def verify_token(self, id_token: str) -> AuthenticatedUser:
        from firebase_admin import auth   # deferred import: keeps boot fast

        decoded = auth.verify_id_token(id_token)
        return AuthenticatedUser(
            uid=decoded["uid"],
            email=decoded.get("email", ""),
        )


# ---------------------------------------------------------------- secrets

class SecretsProvider(Protocol):
    def get(self, name: str) -> str: ...


@dataclass
class EnvSecretsProvider:
    """Dev: .env / process env. Prod: Secret Manager (same interface)."""
    def get(self, name: str) -> str:
        import os
        v = os.environ.get(name)
        if v is None:
            raise KeyError(f"missing secret/env var: {name}")
        return v
