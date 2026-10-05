"""Smoke tests for the seavix-ports workspace package."""
import asyncio

from seavix_ports import (
    InMemoryEventBus,
    LocalBlobStore,
    domain_event,
)


def test_in_memory_event_bus_subscribe_publish():
    bus = InMemoryEventBus()
    received = []
    bus.subscribe("org.created", lambda e: received.append(e))

    event = domain_event("org.created", "organization", "org-1", {"name": "Acme"})
    asyncio.run(bus.publish("org.created", event))

    assert received == [event]
    assert ("org.created", event) in bus.published


def test_in_memory_event_bus_no_cross_topic_delivery():
    bus = InMemoryEventBus()
    received = []
    bus.subscribe("org.created", received.append)

    asyncio.run(bus.publish("other.topic", {"x": 1}))

    assert received == []
    assert len(bus.published) == 1


def test_local_blob_store_presign_paths(tmp_path):
    store = LocalBlobStore(root=tmp_path / "blobs")

    upload_url = asyncio.run(store.presign_upload("a/b.txt"))
    download_url = asyncio.run(store.presign_download("a/b.txt"))

    assert upload_url.startswith("local://")
    assert download_url.startswith("local://")
    assert upload_url.endswith("a/b.txt")
    assert "blobs" in upload_url


def test_domain_event_envelope_shape():
    event = domain_event("org.created", "organization", "org-42", {"name": "Acme"})

    assert set(event) == {
        "id",
        "type",
        "aggregate",
        "aggregate_id",
        "occurred_at",
        "payload",
    }
    assert event["type"] == "org.created"
    assert event["aggregate"] == "organization"
    assert event["aggregate_id"] == "org-42"
    assert event["payload"] == {"name": "Acme"}
    # occurred_at is ISO-8601 UTC
    assert event["occurred_at"].endswith("+00:00")
