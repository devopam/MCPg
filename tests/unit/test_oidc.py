"""Tests for OIDC bearer-token validation (Shortlist 6.5)."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcpg.oidc import (
    ALLOWED_ALGORITHMS,
    OIDCError,
    OIDCVerifier,
    VerifiedToken,
)

# --- helpers -------------------------------------------------------------


def _build_rsa_key() -> tuple[Any, str, dict[str, Any]]:
    """Build an RSA keypair + the JWK that should sign-verify against it."""
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    kid = "test-kid-1"
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return private_key, kid, jwk


def _make_jwt(
    private_key: Any,
    kid: str,
    *,
    issuer: str,
    audience: str,
    extra_claims: dict[str, Any] | None = None,
    exp_offset: int = 3600,
) -> str:
    payload = {
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
        "sub": "user-42",
        **(extra_claims or {}),
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


def _mock_httpx_responses(*, discovery: dict[str, Any], jwks: dict[str, Any]):
    """Patch httpx.AsyncClient.get to return either the discovery or JWKS doc.

    The PyJWKClient uses ``urllib.request`` rather than httpx for the
    JWKS fetch, so we patch BOTH paths.
    """

    class _AsyncResponse:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._body

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: Any) -> _AsyncResponse:
            if url.endswith("/.well-known/openid-configuration"):
                return _AsyncResponse(discovery)
            return _AsyncResponse(jwks)

    # PyJWKClient under the hood goes via urllib.request.urlopen for
    # the JWKS fetch — patch that to return the JSON we want.
    class _UrllibResponse:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = json.dumps(body).encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _UrllibResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    def _urlopen(_url: Any, *args: Any, **kwargs: Any) -> _UrllibResponse:
        return _UrllibResponse(jwks)

    return patch.multiple(
        "mcpg.oidc",
        httpx=type("S", (), {"AsyncClient": _AsyncClient, "HTTPError": httpx.HTTPError}),
    ), patch("urllib.request.urlopen", _urlopen)


# --- tests ---------------------------------------------------------------


def test_allowed_algorithms_are_asymmetric_only() -> None:
    """HS-family (shared-secret) algorithms must never sneak in — that
    would defeat the OIDC trust model."""
    for algo in ALLOWED_ALGORITHMS:
        assert algo.startswith(("RS", "ES")), algo


def test_verifier_init_rejects_blank_issuer_or_audience() -> None:
    with pytest.raises(OIDCError, match="issuer"):
        OIDCVerifier(issuer="", audience="aud")
    with pytest.raises(OIDCError, match="audience"):
        OIDCVerifier(issuer="https://issuer.example", audience="")


def test_verifier_passes_lifespan_and_cache_cap_to_pyjwkclient() -> None:
    """Regression for deep-review scalability P1 #8: PyJWKClient was
    constructed with ``cache_keys=True`` and no other knobs, so the
    in-process key cache had no TTL — an upstream key-rotation event
    required a server restart to pick up. The fix pins
    ``lifespan=jwks_cache_seconds`` (project's 1h default) and
    ``max_cached_keys=16`` so a PyJWKClient default change can't
    quietly grow the in-process set."""
    from unittest.mock import patch

    constructed: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, url: str, **kwargs: Any) -> None:
            constructed.append({"url": url, **kwargs})
            self.uri = url

    async def _trigger_construction() -> None:
        verifier = OIDCVerifier(
            issuer="https://issuer.example",
            audience="mcpg",
            jwks_url="https://issuer.example/jwks.json",
            jwks_cache_seconds=900.0,
        )
        # _ensure_jwks_client is the construction point; calling it
        # directly avoids the discovery + token-verify network paths.
        with patch("mcpg.oidc.PyJWKClient", _FakeClient):
            await verifier._ensure_jwks_client()

    import asyncio

    asyncio.run(_trigger_construction())

    assert constructed, "expected PyJWKClient to be constructed"
    kwargs = constructed[0]
    assert kwargs["cache_keys"] is True
    assert kwargs["lifespan"] == 900  # int-coerced from float setting
    assert kwargs["max_cached_keys"] == 16


async def test_verifier_verifies_a_valid_jwt_against_the_jwks() -> None:
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}

    token = _make_jwt(private_key, kid, issuer=issuer, audience=audience)

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        verifier = OIDCVerifier(issuer=issuer, audience=audience)
        verified = await verifier.verify(token)

    assert isinstance(verified, VerifiedToken)
    assert verified.claims["sub"] == "user-42"
    assert verified.role is None  # no role_claim configured


async def test_verifier_extracts_role_claim_when_configured() -> None:
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}

    token = _make_jwt(
        private_key,
        kid,
        issuer=issuer,
        audience=audience,
        extra_claims={"pg_role": "tenant_42"},
    )

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        verifier = OIDCVerifier(issuer=issuer, audience=audience, role_claim="pg_role")
        verified = await verifier.verify(token)

    assert verified.role == "tenant_42"


async def test_verifier_rejects_role_claim_outside_the_allowlist() -> None:
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}

    token = _make_jwt(
        private_key,
        kid,
        issuer=issuer,
        audience=audience,
        extra_claims={"pg_role": "tenant_zzz"},
    )

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        verifier = OIDCVerifier(
            issuer=issuer,
            audience=audience,
            role_claim="pg_role",
            allowed_roles=("tenant_a", "tenant_b"),
        )
        with pytest.raises(OIDCError, match="not allowed"):
            await verifier.verify(token)


async def test_verifier_rejects_expired_token() -> None:
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}

    # exp 1 hour in the past.
    token = _make_jwt(private_key, kid, issuer=issuer, audience=audience, exp_offset=-3600)

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        verifier = OIDCVerifier(issuer=issuer, audience=audience)
        with pytest.raises(OIDCError, match="expired"):
            await verifier.verify(token)


async def test_verifier_rejects_wrong_audience() -> None:
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience_configured = "mcpg"
    audience_in_token = "some-other-app"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}

    token = _make_jwt(private_key, kid, issuer=issuer, audience=audience_in_token)

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        verifier = OIDCVerifier(issuer=issuer, audience=audience_configured)
        with pytest.raises(OIDCError, match="audience"):
            await verifier.verify(token)


async def test_verifier_rejects_empty_token() -> None:
    verifier = OIDCVerifier(issuer="https://issuer.example", audience="mcpg")
    with pytest.raises(OIDCError, match="empty"):
        await verifier.verify("")


async def test_verifier_propagates_discovery_failure() -> None:
    issuer = "https://issuer.example"

    class _BrokenClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BrokenClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, _url: str, **_kwargs: Any) -> Any:
            raise httpx.ConnectError("DNS failure", request=None)

    with patch(
        "mcpg.oidc.httpx",
        type("S", (), {"AsyncClient": _BrokenClient, "HTTPError": httpx.HTTPError}),
    ):
        verifier = OIDCVerifier(issuer=issuer, audience="mcpg")
        with pytest.raises(OIDCError, match="OIDC discovery failed"):
            await verifier.verify("does-not-matter")


async def test_verifier_uses_explicit_jwks_url_when_provided() -> None:
    """The JWKS-URL override skips discovery — useful when the issuer's
    discovery doc is on a private network but the JWKS is public."""
    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"
    explicit_jwks = "https://other-host.example/keys"

    discovery_calls: list[str] = []

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: Any) -> Any:
            discovery_calls.append(url)
            raise httpx.ConnectError("would fail", request=None)

    class _UrllibResponse:
        def read(self) -> bytes:
            return json.dumps({"keys": [jwk]}).encode()

        def __enter__(self) -> _UrllibResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    token = _make_jwt(private_key, kid, issuer=issuer, audience=audience)

    with (
        patch("mcpg.oidc.httpx", type("S", (), {"AsyncClient": _AsyncClient, "HTTPError": httpx.HTTPError})),
        patch("urllib.request.urlopen", lambda *a, **k: _UrllibResponse()),
    ):
        verifier = OIDCVerifier(issuer=issuer, audience=audience, jwks_url=explicit_jwks)
        verified = await verifier.verify(token)
    # Discovery URL was never hit because we supplied jwks_url.
    assert discovery_calls == []
    assert verified.claims["sub"] == "user-42"


async def test_verifier_offloads_jwks_fetch_to_a_worker_thread() -> None:
    """Regression: PyJWKClient.get_signing_key_from_jwt does sync
    urllib I/O on cache miss; running it directly on the event loop
    blocks every other in-flight request. Pin that we wrap the call
    in asyncio.to_thread so a cache-miss runs off-loop."""
    import asyncio as _asyncio
    import unittest.mock as _mock

    private_key, kid, jwk = _build_rsa_key()
    issuer = "https://issuer.example"
    audience = "mcpg"

    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    jwks = {"keys": [jwk]}
    token = _make_jwt(private_key, kid, issuer=issuer, audience=audience)

    httpx_patch, urlopen_patch = _mock_httpx_responses(discovery=discovery, jwks=jwks)
    with httpx_patch, urlopen_patch:
        # Wrap asyncio.to_thread to confirm it's called with the
        # PyJWKClient method — that's how we ensure the blocking
        # call doesn't run on the event loop.
        original_to_thread = _asyncio.to_thread
        observed: list[str] = []

        async def _spy_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
            observed.append(fn.__name__)
            return await original_to_thread(fn, *args, **kwargs)

        with _mock.patch("mcpg.oidc.asyncio.to_thread", _spy_to_thread):
            verifier = OIDCVerifier(issuer=issuer, audience=audience)
            verified = await verifier.verify(token)

    assert verified.claims["sub"] == "user-42"
    # PyJWKClient.get_signing_key_from_jwt is the method we offloaded.
    assert "get_signing_key_from_jwt" in observed
