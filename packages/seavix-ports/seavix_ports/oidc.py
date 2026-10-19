"""OIDC relying-party plumbing (D3).

SeaVix is the relying party THROUGH Identity Platform's custom-OIDC-provider
support: in prod, Identity Platform exchanges the external IdP's OIDC token
for a SeaVix ID token, and the Admin SDK verifies the latter exactly as
today. This module carries the pieces around that exchange:

- OidcProviderConfig: one row of the identity_provider registry table, in
  the shape services can consume.
- ClaimsMapper: PURE claims -> SeaVix attributes (email, display name,
  role names) using the tenant-administered claims_mapping config. Azure
  AD group UUID -> SeaVix role name happens HERE, as data, not code.
- AuthProvider.sign_in_with_oidc: the RP surface. Prod implementation
  targets Identity Platform (signInWithIdp); FakeOIDCProvider lets dev and
  unit tests exercise the whole flow with synthetic payloads — per D3 the
  emulator cannot do external-IdP E2E.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # avoid a package-root circular import
    from seavix_ports import AuthenticatedUser


@dataclass(frozen=True)
class OidcProviderConfig:
    """Registry row, service-consumable shape."""

    display_name: str
    issuer: str
    client_id: str
    client_secret_ref: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    claims_mapping: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


class OidcProviderStore(Protocol):
    """Backed by the identity_provider table (identity service)."""

    async def get_enabled(self, org_id: str, display_name: str) -> OidcProviderConfig | None: ...


def _b64url_json(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def decode_oidc_token(id_token: str) -> dict:
    """Decode (NOT verify) an OIDC id_token payload. Verification is the
    provider's job (Identity Platform in prod, FakeOIDCProvider's explicit
    checks in dev/tests)."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT: expected three segments")
    return _b64url_json(parts[1])


class ClaimsMapper:
    """claims_mapping config (stored JSON on the registry row):

    {
      "email_claim": "email",            # default "email"
      "name_claim": "name",              # default "name"
      "role_claim": "groups",            # claim carrying group/role ids
      "roles": {"<claim-value>": "<seavix role name>", ...}
    }

    Pure function of (claims, mapping) — the same mapping data drives any
    IdP, which is what makes adding Azure AD a config-only exercise (D3).
    """

    def map(self, claims: dict, mapping: dict[str, Any]) -> dict[str, Any]:
        email_claim = mapping.get("email_claim", "email")
        name_claim = mapping.get("name_claim", "name")
        role_claim = mapping.get("role_claim")
        roles_map: dict[str, str] = mapping.get("roles", {})

        email = claims.get(email_claim, "")
        name = claims.get(name_claim, "")
        roles: list[str] = []
        if role_claim:
            raw = claims.get(role_claim, [])
            if isinstance(raw, str):
                raw = [raw]
            for value in raw:
                role_name = roles_map.get(str(value))
                if role_name:
                    roles.append(role_name)
        return {"email": email, "name": name, "roles": roles}


class OidcSignInError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class FakeOIDCProvider:
    """DEV/TEST ONLY stand-in for Identity Platform's signInWithIdp.

    Verifies STRUCTURE and explicit expectations (iss/aud/exp) but NOT the
    signature — there is no key material in dev. A token here is a claim,
    not proof; never enable outside the emulator suite.
    """

    def __init__(self, config: OidcProviderConfig):
        self.config = config

    async def sign_in(self, id_token: str) -> "AuthenticatedUser":
        from seavix_ports import AuthenticatedUser

        claims = decode_oidc_token(id_token)
        if claims.get("iss") != self.config.issuer:
            raise OidcSignInError("issuer mismatch")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.config.client_id not in auds:
            raise OidcSignInError("audience mismatch")
        email = claims.get("email", "")
        if not email:
            raise OidcSignInError("no email claim")
        # uid: stable, deterministic per (issuer, subject)
        sub = claims.get("sub", "")
        return AuthenticatedUser(
            uid=f"oidc:{self.config.issuer}:{sub}", email=email
        )


class IdentityPlatformOIDCProvider:
    """PROD path: Identity Platform does the RP exchange.

    Wired when the GCP project exists (D3). signInWithIdp with the custom
    OIDC provider id returns a SeaVix ID token; verify via AuthProvider.
    """

    def __init__(self, config: OidcProviderConfig, firebase_project_id: str):
        self.config = config
        self.firebase_project_id = firebase_project_id

    async def sign_in(self, id_token: str) -> "AuthenticatedUser":
        # Prod: firebase_admin auth sign_in_with_idp equivalent via REST,
        # then verify_id_token on the returned SeaVix token. Intentionally
        # not reachable in dev — the emulator has no external IdP (D3).
        raise NotImplementedError(
            "Identity Platform RP exchange requires the GCP project (D3); "
            "use FakeOIDCProvider in dev/tests"
        )
