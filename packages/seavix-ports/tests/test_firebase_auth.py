"""FirebaseAuthProvider against the local Auth emulator.

Flow: create a user via the emulator REST API (accounts:signUp, which also
mints an id_token), then verify that token through the seavix_ports
FirebaseAuthProvider and assert the uid/email round-trip.
"""
import asyncio
import json
import urllib.request
import uuid

import pytest

EMULATOR = "localhost:9099"
PROJECT = "seavix-dev"


@pytest.fixture()
def firebase_app(monkeypatch):
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", EMULATOR)
    import firebase_admin

    try:
        return firebase_admin.get_app()
    except ValueError:
        return firebase_admin.initialize_app(options={"projectId": PROJECT})


def _sign_up(email: str, password: str) -> dict:
    req = urllib.request.Request(
        f"http://{EMULATOR}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=demo-any-key",
        data=json.dumps(
            {"email": email, "password": password, "returnSecureToken": True}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_verify_token_roundtrip(firebase_app):
    from seavix_ports import AuthenticatedUser, FirebaseAuthProvider

    email = f"user-{uuid.uuid4().hex[:10]}@example.dev"
    signed = _sign_up(email, "not-a-real-secret-123")

    provider = FirebaseAuthProvider(project_id=PROJECT)
    user = asyncio.run(provider.verify_token(signed["idToken"]))

    assert isinstance(user, AuthenticatedUser)
    assert user.uid == signed["localId"]
    assert user.email == email


def test_verify_token_rejects_garbage(firebase_app):
    from seavix_ports import FirebaseAuthProvider

    provider = FirebaseAuthProvider(project_id=PROJECT)
    with pytest.raises(Exception):
        asyncio.run(provider.verify_token("not.a.token"))
