"""OIDC / JWT bearer-token validation for the HTTP transport.

When ``MCPG_AUTH_MODE=oidc`` the bearer-token middleware shifts from
constant-time string compare against ``MCPG_HTTP_AUTH_TOKEN`` to full
JWT validation against the configured OIDC provider's JWKS:

* The provider's discovery document (``<issuer>/.well-known/openid-
  configuration``) is fetched on first use and cached. The discovery
  doc points at the JWKS URL, which is fetched and cached
  (:data:`DEFAULT_JWKS_CACHE_SECONDS`).
* Each request's JWT is decoded — signature checked against the JWKS
  key whose ``kid`` matches the JWT header, plus ``exp`` / ``nbf`` /
  ``iss`` / ``aud`` claims validated.
* On verification failure the middleware emits a ``401`` with a
  short reason; the actual exception is logged at WARNING with the
  client IP redacted to keep ops dashboards useful.
* If ``MCPG_OIDC_ROLE_CLAIM`` is set, the value of that claim becomes
  the per-request PG role (composes with the Phase-1.4 tenancy
  driver) — typical setups map a custom claim like ``pg_role`` or
  the standard ``preferred_username`` to a Postgres role name.

Algorithms allowed by default match what the OIDC standard mandates
plus the asymmetric ones Postgres-shaped deployments tend to use
(:data:`ALLOWED_ALGORITHMS`). HS-family algorithms are excluded —
they'd require a shared secret, defeating the OIDC trust model.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0
DEFAULT_JWKS_CACHE_SECONDS = 3600.0
DEFAULT_VERIFY_LEEWAY_SECONDS = 30.0


class OIDCError(Exception):
    """Raised when OIDC configuration is wrong or a token fails to verify."""


# Hostnames that are considered local-only and therefore acceptable
# over plaintext http://. Anything else MUST be https:// — see
# _enforce_https_or_localhost. IPv6 literals are written without
# brackets here; urlsplit().hostname normalises ``[::1]`` to ``::1``.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _enforce_https_or_localhost(url: str, kind: str) -> None:
    """Refuse plaintext ``http://`` for a remote OIDC endpoint.

    ``issuer`` and ``jwks_url`` both end up as URLs the verifier
    fetches signing keys from; an attacker on the path can swap the
    response and forge any token. The carve-out for localhost
    matches industry practice (Keycloak's dev mode, the local
    smoke-test stub at tests/integration/) so the boundary stays
    strict without breaking developer ergonomics.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise OIDCError(f"{kind} is not a valid URL: {url!r}") from exc
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return
    raise OIDCError(
        f"{kind} must use https:// (got scheme={parts.scheme!r}, host={parts.hostname!r}). "
        f"Plaintext OIDC endpoints let a path attacker swap the signing-key set; "
        f"http://localhost is the only exception."
    )


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """Result of a successful :func:`OIDCVerifier.verify` call.

    ``role`` is set when ``role_claim`` is configured AND the JWT
    carried that claim — otherwise ``None`` and the middleware falls
    back to ``MCPG_DEFAULT_ROLE``.
    """

    claims: dict[str, Any]
    role: str | None


@dataclass(slots=True)
class _DiscoveryCache:
    jwks_uri: str
    fetched_at: float


class OIDCVerifier:
    """Verifies JWTs against an OIDC provider's JWKS.

    Construction is cheap — no network I/O. The discovery + JWKS
    fetch happen on first :meth:`verify` call and cache for
    :data:`DEFAULT_JWKS_CACHE_SECONDS`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        role_claim: str | None = None,
        allowed_roles: tuple[str, ...] = (),
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        jwks_cache_seconds: float = DEFAULT_JWKS_CACHE_SECONDS,
        verify_leeway: float = DEFAULT_VERIFY_LEEWAY_SECONDS,
    ) -> None:
        if not issuer:
            raise OIDCError("issuer must not be blank")
        if not audience:
            raise OIDCError("audience must not be blank")
        # Discovery joins ``issuer + "/.well-known/openid-configuration"``
        # and an explicit ``jwks_url`` is fetched verbatim. Either over
        # plaintext silently downgrades signing-key trust — an attacker
        # on the path can swap the JWKS for keys they control and forge
        # tokens MCPg will then accept. Refuse non-https schemes at the
        # boundary, with one carve-out for ``http://localhost``-shaped
        # addresses so dev / test setups (Keycloak in-Docker, a stub
        # IdP on a port) keep working without weakening prod.
        _enforce_https_or_localhost(issuer, "issuer")
        if jwks_url is not None:
            _enforce_https_or_localhost(jwks_url, "jwks_url")
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._explicit_jwks_url = jwks_url
        self._role_claim = role_claim
        self._allowed_roles = frozenset(allowed_roles)
        self._discovery_timeout = discovery_timeout
        self._jwks_cache_seconds = jwks_cache_seconds
        self._verify_leeway = verify_leeway

        self._discovery: _DiscoveryCache | None = None
        self._jwks_client: PyJWKClient | None = None

    async def _resolve_jwks_url(self) -> str:
        """Return the JWKS URL — explicit override wins, else discovery."""
        if self._explicit_jwks_url is not None:
            return self._explicit_jwks_url
        if self._discovery is not None and (time.monotonic() - self._discovery.fetched_at < self._jwks_cache_seconds):
            return self._discovery.jwks_uri
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=self._discovery_timeout) as client:
                response = await client.get(url)
            response.raise_for_status()
            doc = response.json()
        except Exception as exc:
            raise OIDCError(f"OIDC discovery failed at {url}: {exc}") from exc
        jwks_uri = doc.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise OIDCError(f"OIDC discovery doc at {url} has no jwks_uri")
        self._discovery = _DiscoveryCache(jwks_uri=jwks_uri, fetched_at=time.monotonic())
        return jwks_uri

    async def _ensure_jwks_client(self) -> PyJWKClient:
        url = await self._resolve_jwks_url()
        # PyJWKClient caches keys in-process; reuse the same client
        # for the JWKS-URL lifetime. Recreate when the URL changes
        # (e.g. discovery doc rotated).
        #
        # ``lifespan`` is the cache TTL in seconds for PyJWKClient's
        # signing-key cache. We pass our project-configured value
        # (``DEFAULT_JWKS_CACHE_SECONDS`` = 1h) so an upstream
        # key-rotation event is picked up at most one TTL after it
        # publishes — operators don't need a server restart any more.
        # ``max_cached_keys`` defaults to 16 inside PyJWKClient, which
        # is generous for a single-issuer setup; we pin it explicitly
        # so a future PyJWKClient default change can't quietly grow
        # the in-process key set.
        if self._jwks_client is None or getattr(self._jwks_client, "uri", None) != url:
            self._jwks_client = PyJWKClient(
                url,
                cache_keys=True,
                max_cached_keys=16,
                lifespan=int(self._jwks_cache_seconds),
            )
        return self._jwks_client

    async def verify(self, token: str) -> VerifiedToken:
        """Validate ``token`` and return its claims + optional role.

        Raises :class:`OIDCError` on any verification failure — caller
        translates that into a ``401`` for the client.
        """
        if not token:
            raise OIDCError("empty token")
        client = await self._ensure_jwks_client()
        # PyJWKClient.get_signing_key_from_jwt fetches the JWKS via
        # urllib.request (synchronous, blocking) on the first call /
        # whenever the cache misses. Run it on a worker thread so a
        # cache-miss can't stall the ASGI event loop for every other
        # in-flight request.
        try:
            signing_key_obj = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
            signing_key = signing_key_obj.key
        except Exception as exc:
            raise OIDCError(f"could not resolve signing key: {exc}") from exc
        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._verify_leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OIDCError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise OIDCError("invalid audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise OIDCError("invalid issuer") from exc
        except jwt.InvalidTokenError as exc:
            raise OIDCError(f"invalid token: {exc}") from exc

        role: str | None = None
        if self._role_claim is not None:
            raw = claims.get(self._role_claim)
            if raw is not None:
                role = str(raw)
                if self._allowed_roles and role not in self._allowed_roles:
                    raise OIDCError(f"role {role!r} from claim {self._role_claim!r} is not allowed")
        return VerifiedToken(claims=claims, role=role)
