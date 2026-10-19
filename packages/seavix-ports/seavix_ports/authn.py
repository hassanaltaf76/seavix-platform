"""Deny-by-default authentication — port surface shared by every service.

Pieces:
- AuthContext / MembershipContext: the caller's resolved identity, org
  memberships, roles, and EFFECTIVE permissions (union across roles; task 22
  fills the role tables — the shapes are final now).
- AuthContextLoader protocol: each service injects HOW credentials resolve
  (identity: its own database; web: the identity service over HTTP).
- public_route decorator: the ONLY way a route opts out of the wall.
- AuthMiddleware: pure-ASGI deny-by-default wall. Unmarked route + no
  usable credential -> 401 (JSON) or a configured redirect. The resolved
  context lands in scope["seavix.auth"] for the route to consume.
- require_auth: FastAPI dependency reading the middleware-resolved context.

Failure posture everywhere: fail CLOSED. Any loader exception, missing
credential, or expired session is indistinguishable from "no auth" — an
unmarked route never reveals whether the path exists.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("seavix.ports.authn")

SCOPE_KEY = "seavix.auth"
SESSION_HEADER = "X-Seavix-Session"
PUBLIC_ATTR = "__seavix_public__"


def _public_marker():
    def _mount() -> None: ...

    setattr(_mount, PUBLIC_ATTR, True)
    return _mount


_PUBLIC_MOUNT = _public_marker()  # sentinel: static mounts are always public


# ------------------------------------------------------------------ context

@dataclass(frozen=True)
class MembershipContext:
    org_id: str
    slug: str
    org_type: str
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuthContext:
    user: Any  # AuthenticatedUser (declared Any to avoid a circular import)
    email_verified: bool = False
    memberships: tuple[MembershipContext, ...] = ()

    @property
    def roles(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.memberships:
            for r in m.roles:
                if r not in seen:
                    seen.append(r)
        return tuple(seen)

    @property
    def permissions(self) -> frozenset[str]:
        """Effective permissions: UNION across roles and memberships.
        Deny does not override allow at this layer (documented decision)."""
        out: set[str] = set()
        for m in self.memberships:
            out |= set(m.permissions)
        return frozenset(out)

    def membership_for(self, org_id: str) -> MembershipContext | None:
        for m in self.memberships:
            if m.org_id == org_id:
                return m
        return None


# ------------------------------------------------------------------ loader

class AuthContextLoader(Protocol):
    """Service-injected credential resolution. Return None (or raise) for
    any invalid/expired credential — the middleware fails closed."""

    async def load_from_bearer(self, token: str) -> AuthContext | None: ...

    async def load_from_session(self, session_id: str) -> AuthContext | None: ...


# --------------------------------------------------------- public marking

def public_route(endpoint):
    """Opt a route OUT of the deny-by-default wall. Use UNDER the router
    decorator so the marked function is the registered endpoint:

        @router.get("/login")
        @public_route
        async def login(...): ...
    """
    setattr(endpoint, PUBLIC_ATTR, True)
    return endpoint


def is_public(endpoint: Any) -> bool:
    return bool(getattr(endpoint, PUBLIC_ATTR, False))


def iter_leaf_routes(routes: Any):
    """Yield concrete Route objects.

    Starlette 1.x keeps included routers as lazy `_IncludedRouter` wrappers
    (`.original_router` holds the real routes); plain `Router`s nest too.
    Mounts are yielded as-is — the middleware treats them as public
    infrastructure."""
    from starlette.routing import Mount

    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_leaf_routes(inner.routes)
        elif isinstance(route, Mount):
            yield route
        elif hasattr(route, "routes") and not hasattr(route, "path"):
            yield from iter_leaf_routes(route.routes)
        else:
            yield route


# ---------------------------------------------------------------- middleware

class AuthMiddleware:
    """App-wide deny-by-default wall.

    - Routes whose endpoint carries the public mark pass through untouched.
    - Everything else needs a Bearer token or a session credential; the
      loader's AuthContext is stored in scope["seavix.auth"].
    - unauthenticated="json" -> 401 JSON; "redirect:<path>" -> 303 redirect
      (server-rendered UX on the web app).
    """

    def __init__(
        self,
        app: ASGIApp,
        loader: AuthContextLoader,
        *,
        cookie: str | None = None,
        unauthenticated: str = "json",
        route_source: Any | None = None,
    ) -> None:
        self.app = app
        self.loader = loader
        self.cookie = cookie
        self.unauthenticated = unauthenticated
        # The FastAPI app, read lazily: at request time its router knows
        # every route. (self.app is the wrapped ASGI layer — ExceptionMiddleware
        # — which does not expose the router.)
        self.route_source = route_source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        endpoint = self._match_endpoint(scope)
        if endpoint is not None and is_public(endpoint):
            await self.app(scope, receive, send)
            return

        credential = self._extract(scope)
        if credential is None:
            await self._deny(scope, receive, send)
            return

        kind, value = credential
        try:
            if kind == "bearer":
                ctx = await self.loader.load_from_bearer(value)
            else:
                ctx = await self.loader.load_from_session(value)
        except Exception:
            logger.debug("auth loader rejected credential", exc_info=True)
            ctx = None
        if ctx is None:
            await self._deny(scope, receive, send)
            return

        scope = dict(scope)
        scope[SCOPE_KEY] = ctx
        await self.app(scope, receive, send)

    # -- internals
    def _match_endpoint(self, scope: Scope) -> Any | None:
        """Resolve the request to its endpoint function via the wrapped
        app's router. Mounts (static assets) are public infrastructure.
        Returns None for unmatched paths (treated protected: fail closed,
        the router's own 404 never leaks unauthenticated)."""
        from starlette.routing import Mount

        routes = None
        if self.route_source is not None:
            routes = self.route_source.routes
        else:
            router = getattr(self.app, "router", None)
            routes = router.routes if router is not None else None
        if routes is None:
            return None
        for route in iter_leaf_routes(routes):
            match, child_scope = route.matches(scope)
            if match.name != "FULL":
                continue
            if isinstance(route, Mount):
                return _PUBLIC_MOUNT
            return child_scope.get("endpoint")
        return None

    def _extract(self, scope: Scope) -> tuple[str, str] | None:
        headers = dict(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth.removeprefix("Bearer ").strip():
            return ("bearer", auth.removeprefix("Bearer ").strip())
        sid = headers.get(SESSION_HEADER.lower(), "").strip()
        if not sid and self.cookie:
            cookie_header = headers.get("cookie", "")
            sid = self._cookie_value(cookie_header, self.cookie)
        if sid:
            return ("session", sid)
        return None

    @staticmethod
    def _cookie_value(header: str, name: str) -> str:
        for part in header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v.strip()
        return ""

    async def _deny(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.unauthenticated.startswith("redirect:"):
            target = self.unauthenticated.removeprefix("redirect:")
            response: Response = RedirectResponse(target, status_code=303)
        else:
            response = Response(
                json.dumps({"detail": "authentication required"}),
                status_code=401,
                media_type="application/json",
            )
        await response(scope, receive, send)


# ---------------------------------------------------------------- dependency

def require_auth(request: Request) -> AuthContext:
    """FastAPI dependency for protected routes. The middleware has already
    resolved and validated the context — this never re-verifies."""
    ctx = request.scope.get(SCOPE_KEY)
    if ctx is None:
        # Only reachable when the middleware is not installed (tests that
        # call endpoints in isolation) — fail closed either way.
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="authentication required")
    return ctx


def optional_auth(request: Request) -> AuthContext | None:
    return request.scope.get(SCOPE_KEY)


# ------------------------------------------------------------- serialization

def context_to_dict(ctx: AuthContext) -> dict:
    """Wire format between identity (producer) and other services (web)."""
    from seavix_ports import AuthenticatedUser

    user: AuthenticatedUser = ctx.user
    return {
        "uid": user.uid,
        "email": user.email,
        "email_verified": ctx.email_verified,
        "memberships": [
            {
                "org_id": m.org_id,
                "slug": m.slug,
                "org_type": m.org_type,
                "roles": list(m.roles),
                "permissions": sorted(m.permissions),
            }
            for m in ctx.memberships
        ],
    }


def context_from_dict(data: dict) -> AuthContext:
    from seavix_ports import AuthenticatedUser

    return AuthContext(
        user=AuthenticatedUser(uid=data["uid"], email=data.get("email", "")),
        email_verified=bool(data.get("email_verified", False)),
        memberships=tuple(
            MembershipContext(
                org_id=m["org_id"],
                slug=m["slug"],
                org_type=m["org_type"],
                roles=tuple(m.get("roles", ())),
                permissions=frozenset(m.get("permissions", ())),
            )
            for m in data.get("memberships", ())
        ),
    )
