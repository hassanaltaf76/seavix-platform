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


# ---------------------------------------------------------------- authz

@dataclass(frozen=True)
class AuthzTuple:
    user: str
    relation: str
    object: str


class AuthzPort(Protocol):
    async def check(self, user: str, relation: str, object: str) -> bool: ...
    async def write_tuples(self, tuples: list) -> None: ...
    async def delete_tuples(self, tuples: list) -> None: ...


@dataclass
class AuthzClient:
    """OpenFGA HTTP implementation.

    Talks to the OpenFGA HTTP API at http_addr (current OpenFGA serves the
    API WITHOUT the /v1 prefix: /stores/{id}/check, /stores/{id}/write).
    Deletes go through POST /write with a "deletes" key — the dedicated
    /delete endpoint does not exist (verified: undefined_endpoint).

    Prod swap: same class against the managed OpenFGA endpoint, or a
    different implementation of AuthzPort — services never call HTTP directly.
    """

    store_id: str
    http_addr: str = "localhost:18080"

    @classmethod
    def from_env(cls) -> "AuthzClient":
        import os

        store_id = os.environ.get("OPENFGA_STORE_ID")
        if not store_id:
            raise KeyError("missing env var: OPENFGA_STORE_ID")
        return cls(
            store_id=store_id,
            http_addr=os.environ.get("OPENFGA_HTTP_ADDR", "localhost:18080"),
        )

    async def check(self, user: str, relation: str, object: str) -> bool:
        body = await self._post(
            f"/stores/{self.store_id}/check",
            {"tuple_key": {"user": user, "relation": relation, "object": object}},
        )
        return bool(body["allowed"])

    async def write_tuples(self, tuples: list) -> None:
        await self._post(
            f"/stores/{self.store_id}/write",
            {"writes": {"tuple_keys": [self._norm(t) for t in tuples]}},
        )

    async def delete_tuples(self, tuples: list) -> None:
        await self._post(
            f"/stores/{self.store_id}/write",
            {"deletes": {"tuple_keys": [self._norm(t) for t in tuples]}},
        )

    # -- helpers
    @staticmethod
    def _norm(t) -> dict:
        if isinstance(t, AuthzTuple):
            return {"user": t.user, "relation": t.relation, "object": t.object}
        return {"user": t["user"], "relation": t["relation"], "object": t["object"]}

    async def _post(self, path: str, payload: dict) -> dict:
        import httpx

        async with httpx.AsyncClient(
            base_url=f"http://{self.http_addr}", timeout=10.0
        ) as client:
            resp = await client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json() if resp.content else {}


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
