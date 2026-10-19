# SeaVix Authentication & Authorization Architecture

Stage 1 (Identity & Access + Authorization) — 2026-10-19. Covers the token
model, the relying-party design for external OIDC identity providers, the
IdP registry and claims→role mapping, the Azure AD phase-B checklist, the
production authN chain, and the RBAC-vs-OpenFGA split. Decisions D1–D4 from
the approved brief are referenced inline.

## a. Token model — what an Identity Platform ID token is and guarantees

SeaVix sessions are built on **Google Identity Platform ID tokens**
(dev: the Firebase Auth emulator — same code paths, env-var flip).

- An ID token is an **OIDC-conformant JWT**: header
  `{"alg":"RS256","typ":"JWT","kid":"<key-id>"}`, payload claims
  `iss` (issuer, e.g. `https://securetoken.google.com/<project>`),
  `aud` (the Firebase project id), `sub` (the stable user id), `auth_time`,
  `exp` (±1h), `iat`, plus Firebase claims (`firebase.identities`,
  `sign_in_provider`, tenant id when using multi-tenancy).
- **What verification guarantees** (Admin SDK `verify_id_token`, the
  `AuthProvider` port): the token was minted by Google for *our project*
  (signature against Google's published JWKS, `iss`/`aud` match), it has
  not expired, and it was issued after the last password-reset / disable
  event (`auth_time` / token revocation checks).
- **What it does not guarantee by itself**: that the holder may act inside
  any SeaVix organization. Authorization is always SeaVix's own decision
  (RBAC in the identity DB + OpenFGA cross-org grants). This is why a valid
  ID token with no membership yields 403, not access.
- Web sessions add a second layer: the browser holds only an **opaque
  session id** (`seavix_session`, HttpOnly, SameSite=Lax) resolved against
  the identity DB on every request (task 20). Revocation — logout,
  deactivation, expiry — is immediate; a stateless JWT cookie could not
  promise that.

## b. SeaVix as a relying party — generic OIDC through Identity Platform

SeaVix never verifies external IdP tokens itself in production. It is the
relying party **via Identity Platform's custom-OIDC-provider support**:

1. An org admin registers an IdP in the `identity_provider` registry
   (§c). One row per (org, IdP).
2. The browser does the standard OIDC dance with the external IdP
   (authorization code + PKCE) and lands on
   `POST /api/public/auth/oidc/callback` with the external `id_token`.
3. SeaVix calls Identity Platform's `signInWithIdp` with the provider id
   built from the registry row. Identity Platform validates the external
   token (signature against the IdP's discovery/JWKS document, `iss`,
   `aud`, expiry), applies its own token-rotation policy, and returns a
   **SeaVix ID token** — the exact artifact §a describes.
4. From here the flow is identical to password+OTP login: session
   issuance (after the OTP second factor when required), AuthContext
   resolution, RBAC, OpenFGA.

The `AuthProvider` port surface carries this: `verify_token` (unchanged,
§a) plus the RP entry point `sign_in_with_oidc` (implemented by
`IdentityPlatformOIDCProvider` in prod, `FakeOIDCProvider` in dev/tests —
see `packages/seavix-ports/seavix_ports/oidc.py`). Because the exchange
produces a SeaVix token, **no SeaVix verification code changes when an
IdP is added** — only registry data (§d).

## c. IdP registry + claims→role mapping (implemented, migration 0006)

`identity_provider` (org-scoped; client secret referenced by name through
the SecretsProvider, never stored):

| column | purpose |
|---|---|
| id | uuid pk |
| org_id | owning organization (unique with display_name) |
| display_name | tenant-chosen label, e.g. "azure-ad" |
| issuer | OIDC issuer URL; validated as the external token's `iss` |
| client_id | RP client id; validated as the external token's `aud` |
| client_secret_ref | SecretsProvider entry for the RP client secret |
| scopes | JSON list, default `["openid","email","profile"]` |
| claims_mapping | JSON config driving the pure mapper (below) |
| enabled | soft switch |

**Claims→role mapping is configuration, not code.** `ClaimsMapper`
(`seavix_ports.oidc`) maps external claims to SeaVix attributes:

```json
{
  "email_claim": "email",
  "name_claim": "name",
  "role_claim": "groups",
  "roles": { "<external group/role value>": "<seavix role name>", ... }
}
```

At first sign-in the callback JIT-provisions the user (`app_user` with a
deterministic `oidc:<issuer>:<sub>` uid), creates the org membership, and
binds the mapped SeaVix roles — role names that do not exist in the org
are skipped and reported (`roles_skipped`). Re-sign-in reuses the user,
refreshes role bindings; an external identity is never silently linked onto
an existing password account (409 until verified-email linking is a prod
decision).

## d. Azure AD (Entra ID) — Phase B checklist, zero application code

When the tenant wants Microsoft sign-in, an org admin (role.manage) does:

1. **Entra app registration** (Entra portal): new app, account type
   "single tenant" (or multitenant per tenant policy). Note the
   **Application (client) ID** and **Directory (tenant) ID**.
2. **Client secret**: create one; store its value in the secret store and
   note the entry name for `client_secret_ref`.
3. **Redirect URIs**: add the SeaVix callback origin
   (`https://<env>.seavix.ai/api/public/auth/oidc/callback` — the SPA-style
   dance uses PKCE against Identity Platform's hosted flow).
4. **ID token claims**: in Token configuration, ensure `email`, `name`,
   and **Groups** are emitted; for groups use **group IDs** (the uuid
   strings) for stability. If the groups claim becomes large, use the
   overage Graph approach — the mapping table still keys on group id.
5. **Registry entry** (one API call, `POST /api/orgs/me/idps`):

   ```json
   {
     "display_name": "azure-ad",
     "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
     "client_id": "<application-id>",
     "client_secret_ref": "IDP_AZURE_AD_CLIENT_SECRET",
     "scopes": ["openid", "email", "profile"],
     "claims_mapping": {
       "email_claim": "email",
       "name_claim": "name",
       "role_claim": "groups",
       "roles": {
         "<entra-group-uuid-for-ops>": "ops",
         "<entra-group-uuid-for-viewers>": "viewer"
       }
     }
   }
   ```

6. **Identity Platform console** (platform admin, once per GCP project):
   Authentication → Sign-in method → Add new provider → **OpenID
   Connect**, provider id `oidc.azure-ad`, client id + secret, issuer URL.
   (This console step is the only piece outside the tenant's own API call;
   automating it via Terraform is the natural follow-up.)

That is all: Azure AD group `X` ⇒ SeaVix role `Y` is row data in
`claims_mapping`; no application code, no migration, no redeploy.

## e. Production authentication chain

```
browser ──HTTPS──▶ IAP (identity-aware proxy: DDoS/WAF, GCP health)
                     │  asserts Google identity (JWT header) or passes through
                     ▼
              Identity Platform (Firebase Auth / GCIP)
                     │  verifies credentials; mints RS256 ID token (§a)
                     │  custom OIDC providers for external IdPs (§b)
                     ▼
              SeaVix AuthMiddleware (deny-by-default, task 21)
                     │  Bearer OR session cookie → AuthContext
                     │  (user, memberships, roles, effective permissions)
                     ▼
              Intra-org authorization: RBAC in the identity DB (task 22)
                     │  permission checks (union semantics) on org-scoped rows
                     ▼
              Cross-org authorization: OpenFGA grants (09b, tasks 13+)
                     │  e.g. client-org access to a report/job in another org
                     ▼
              resource handler (404-over-403 on foreign rows)
```

Supporting pieces already in place: custom email-OTP second factor with
rate limits (D1), opaque sessions for immediate revocation (task 20),
invite-based provisioning (D4, task 23), registration email verification
gating go-live.

## f. Why RBAC (DB) and OpenFGA are both here

The two stores answer different questions:

- **RBAC in the identity database** answers *"may this user, inside their
  own organization, do X?"* — membership → 1..N roles → permission union.
  It is relational, transactional with the data it protects, fast to check
  per request, and versioned with the platform's own migrations. The
  permission catalog is system-owned (D2): tenants compose roles, never
  invent permissions.
- **OpenFGA** answers *"may user U (or org A) touch a specific object in a
  DIFFERENT organization?"* — cross-org grants like a client viewing a
  report, or a subcontracted surveyor accessing one job. These are sparse,
  graph-shaped, revocable grants over object instances — a poor fit for
  relational role tables, and OpenFGA's exact use case (and the reason
  registration still writes the `owner` tuple).

Rule of thumb: if the question contains "in my org" → RBAC. If it contains
"across orgs / this specific report / this one job" → OpenFGA. Deny by
default sits in front of both (task 21); a denied cross-org probe is a 404
(documented policy).

## D3 limitation note (emulator)

External-IdP sign-in cannot be E2E-tested on the Firebase emulator (no
external provider exchange). The full plumbing — registry CRUD, callback,
claims mapping, JIT provisioning, session issuance, role binding — is
unit/integration-tested with synthetic OIDC tokens via `FakeOIDCProvider`,
which checks structure, `iss`, `aud` and email presence but **not
signatures** (dev/test only; never enable elsewhere). When the GCP project
exists: enable the real provider per §d, flip the callback to
`IdentityPlatformOIDCProvider`, and re-run the same tests unchanged —
they assert the plumbing contract, which is identical on both sides of
the exchange.
