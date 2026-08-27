"""Tests for OIDC bearer-token validation (Shortlist 6.5)."""

from __future__ import annotations

import json
import time
from typing import Any, Self
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

# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Reset every registered circuit breaker before/after each test.

    ``@circuit`` decorates ``OIDCVerifier._resolve_jwks_url`` once at class
    definition time, so the breaker's failure count is a single object
    shared across *every* ``OIDCVerifier`` instance for the life of the test
    process — not per-instance state. Without this reset, a test that trips
    the breaker open would leave it open for every unrelated test that runs
    afterward in the same session (including tests in test_nl2sql.py, which
    registers its own separately-named breakers alongside this module's).
    """
    from circuitbreaker import CircuitBreakerMonitor

    for cb in CircuitBreakerMonitor.get_circuits():
        cb.reset()
    yield
    for cb in CircuitBreakerMonitor.get_circuits():
        cb.reset()


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


# C901 rationale: test-only mock-response builder patching both httpx (our
# code) and urllib.request (PyJWKClient's internal transport) with several
# small nested fake classes -- test infrastructure, not production logic.
def _mock_httpx_responses(*, discovery: dict[str, Any], jwks: dict[str, Any]):  # noqa: C901
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

        async def __aenter__(self) -> Self:
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

        def __enter__(self) -> Self:
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


def test_verifier_init_rejects_plaintext_http_issuer() -> None:
    """Regression for deep-review P1 #7: discovery joins issuer +
    /.well-known/openid-configuration. An http:// issuer downloads
    the JWKS over plaintext, so a path attacker can swap keys and
    forge tokens MCPg will then accept. Refuse at the boundary."""
    with pytest.raises(OIDCError, match="https"):
        OIDCVerifier(issuer="http://issuer.example", audience="mcpg")


def test_verifier_init_rejects_plaintext_http_jwks_url() -> None:
    """Same threat model as the issuer case, for the explicit
    jwks_url override path that _resolve_jwks_url returns verbatim."""
    with pytest.raises(OIDCError, match="https"):
        OIDCVerifier(
            issuer="https://issuer.example",
            audience="mcpg",
            jwks_url="http://issuer.example/jwks.json",
        )


def test_verifier_init_allows_http_localhost_for_dev() -> None:
    """Carve-out: http://localhost (and 127.0.0.1, ::1) stays
    accepted so Keycloak-in-Docker / stub-IdP smoke tests keep
    working without weakening the prod posture."""
    # Issuer side.
    OIDCVerifier(issuer="http://localhost:8080/auth/realms/test", audience="mcpg")
    OIDCVerifier(issuer="http://127.0.0.1:8080", audience="mcpg")
    OIDCVerifier(issuer="http://[::1]:8080", audience="mcpg")
    # Explicit jwks_url side.
    OIDCVerifier(
        issuer="https://issuer.example",
        audience="mcpg",
        jwks_url="http://localhost:8080/jwks.json",
    )


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

        async def __aenter__(self) -> Self:
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

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: Any) -> Any:
            discovery_calls.append(url)
            raise httpx.ConnectError("would fail", request=None)

    class _UrllibResponse:
        def read(self) -> bytes:
            return json.dumps({"keys": [jwk]}).encode()

        def __enter__(self) -> Self:
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


# --- shared httpx.AsyncClient reuse (perf audit remediation, Task 9) ------


async def test_verifier_reuses_one_httpx_client_across_discovery_fetches() -> None:
    """The discovery-document fetch reuses one ``httpx.AsyncClient`` held
    for the verifier's whole lifetime, rather than opening ``async with
    httpx.AsyncClient(...)`` fresh on every fetch. ``jwks_cache_seconds=0``
    forces the cache to be treated as expired immediately, so two direct
    ``_resolve_jwks_url()`` calls both actually hit the network path."""
    issuer = "https://issuer.example"
    discovery = {"issuer": issuer, "jwks_uri": f"{issuer}/.well-known/jwks.json"}
    constructed: list[object] = []

    class _AsyncResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return discovery

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(self)

        async def get(self, url: str, **_kwargs: Any) -> _AsyncResponse:
            return _AsyncResponse()

    with patch(
        "mcpg.oidc.httpx",
        type("S", (), {"AsyncClient": _AsyncClient, "HTTPError": httpx.HTTPError}),
    ):
        verifier = OIDCVerifier(issuer=issuer, audience="mcpg", jwks_cache_seconds=0.0)
        url_one = await verifier._resolve_jwks_url()
        url_two = await verifier._resolve_jwks_url()

    assert url_one == url_two == discovery["jwks_uri"]
    assert len(constructed) == 1, f"expected exactly one httpx.AsyncClient construction; got {len(constructed)}"


async def test_verifier_aclose_closes_the_underlying_client() -> None:
    """``aclose()`` closes the verifier's held HTTP client."""
    closed: list[bool] = []

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            closed.append(True)

    with patch(
        "mcpg.oidc.httpx",
        type("S", (), {"AsyncClient": _AsyncClient, "HTTPError": httpx.HTTPError}),
    ):
        verifier = OIDCVerifier(issuer="https://issuer.example", audience="mcpg")
        await verifier.aclose()

    assert closed == [True]


# --- circuit breaker on JWKS discovery (audit remediation, Task 15) -------


async def test_ensure_jwks_client_opens_circuit_after_repeated_discovery_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After enough consecutive discovery-fetch failures, the breaker opens
    and further calls fail fast (as OIDCError, not a bare CircuitBreakerError)
    without hitting the network again.

    Loops only just past the failure threshold (6, not 10) to keep this fast
    — the invariant only needs one call past the point the breaker opens.
    """
    verifier = OIDCVerifier(issuer="https://idp.example", audience="mcpg")
    call_count = 0

    async def _always_fails(_url: str, **_kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("simulated outage", request=None)

    monkeypatch.setattr(verifier._client, "get", _always_fails)

    for _ in range(6):
        with pytest.raises(OIDCError):
            await verifier._ensure_jwks_client()

    # The breaker should have opened well before the 6th iteration — assert
    # relatively (not against an absolute call count), since Task 16 layers
    # retry *inside* the breaker and changes how many real network calls
    # happen per logical failure.
    calls_before_open = call_count
    with pytest.raises(OIDCError):
        await verifier._ensure_jwks_client()
    assert call_count == calls_before_open, "breaker should short-circuit without re-invoking the discovery fetch"


# --- retry with backoff on JWKS discovery (audit remediation, Task 16) ----


async def test_resolve_jwks_url_retries_transient_failures_before_giving_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovery fetch that fails twice then succeeds is retried
    transparently — not immediately surfaced as an error."""
    attempts = 0

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"jwks_uri": "https://idp.example/jwks"}

    async def _flaky_get(_url: str, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("transient", request=None)
        return _FakeResponse()

    verifier = OIDCVerifier(issuer="https://idp.example", audience="mcpg")
    monkeypatch.setattr(verifier._client, "get", _flaky_get)

    url = await verifier._resolve_jwks_url()

    assert attempts == 3
    assert url == "https://idp.example/jwks"
