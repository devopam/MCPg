"""Pluggable secrets backend.

By default MCPg reads every secret straight from the process
environment (``MCPG_SECRETS_BACKEND=env``) — the historical behaviour,
zero new dependencies. Deployments that keep credentials in a mounted
file can set ``MCPG_SECRETS_BACKEND=file`` + ``MCPG_SECRETS_FILE_PATH``
to load a flat ``name -> value`` map of JSON (always) or YAML (when
PyYAML is importable).

A provider only supplies *secret values*; non-secret configuration
still comes from the environment. Every provider overlays its store
on top of the environment — a name present in the backend wins, and
anything absent falls back to the env var — so partial backends and
vendor-conventional API-key env vars keep working.

Cloud backends:

- ``vault`` — HashiCorp Vault KV v2 via the ``hvac`` SDK, behind the
  ``mcpg[vault]`` extra. Configured with ``MCPG_VAULT_ADDR`` /
  ``MCPG_VAULT_TOKEN`` (+ optional ``MCPG_VAULT_NAMESPACE`` /
  ``MCPG_VAULT_PATH_PREFIX``).
- ``aws`` — AWS Secrets Manager via the ``boto3`` SDK, behind the
  ``mcpg[aws]`` extra. Authentication uses the standard AWS env /
  IAM-role chain; the optional ``MCPG_AWS_SECRETS_PREFIX`` is
  prepended to every name on lookup.
- ``gcp`` — Google Cloud Secret Manager via
  ``google-cloud-secret-manager``, behind the ``mcpg[gcp]`` extra.
  ``MCPG_GCP_PROJECT_ID`` is required; ``MCPG_GCP_SECRETS_PREFIX`` is
  the optional name prefix.

Each cloud provider caches lookups in-process for the lifetime of the
server — the latency + per-call cost of a real secrets manager makes
re-fetching on every config touch impractical. Restart the server to
pick up rotated values (or wire the rotation system to do so).
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_SUPPORTED_BACKENDS = frozenset({"env", "file", "vault", "aws", "gcp"})


class SecretsError(Exception):
    """Raised when the secrets backend is misconfigured or unreadable."""


@runtime_checkable
class SecretsProvider(Protocol):
    """Resolves a secret by name, returning the raw value or ``None``.

    Callers keep their own stripping / blank-rejection — ``get`` simply
    returns whatever the backend holds (or ``None`` when absent), so it
    is a drop-in replacement for ``env.get(name)``.
    """

    def get(self, name: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class EnvSecretsProvider:
    """Reads secrets from a mapping (the process environment by default)."""

    # repr=False on env so a stray ``logging.exception(provider)`` or
    # pytest assert can't spill the entire process environment (every
    # API key the operator has set lives in there). The mapping is
    # still accessible via ``.env`` for code that needs it.
    env: Mapping[str, str] = field(repr=False)

    def get(self, name: str) -> str | None:
        return self.env.get(name)


@dataclass(frozen=True, slots=True)
class FileSecretsProvider:
    """Reads secrets from a file overlay, falling back to the environment.

    ``overlay`` is the flat ``name -> value`` map loaded from the
    configured file; a name present there wins, everything else defers
    to ``env`` so non-secret config and unlisted vendor keys still work.
    """

    # Both the file overlay (literal secret material on disk) and the
    # env mapping are kept out of repr; same reasoning as
    # EnvSecretsProvider.
    overlay: Mapping[str, str] = field(repr=False)
    env: Mapping[str, str] = field(repr=False)

    def get(self, name: str) -> str | None:
        if name in self.overlay:
            return self.overlay[name]
        return self.env.get(name)


def _load_overlay(path: str) -> dict[str, str]:
    """Load + validate a flat ``name -> value`` secrets file (JSON / YAML)."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        raise SecretsError(f"could not read MCPG_SECRETS_FILE_PATH ({path!r}): {exc}") from exc

    is_yaml = path.lower().endswith((".yaml", ".yml"))
    data: object
    if is_yaml:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - PyYAML is present in CI
            raise SecretsError(
                f"MCPG_SECRETS_FILE_PATH ({path!r}) is YAML but PyYAML is not installed; "
                "install it (pip install pyyaml) or use a .json secrets file"
            ) from exc
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            # Deliberately surface only the file path + structural position
            # (line/column when available) — the parser's full message can
            # echo source text and would leak secret values into logs.
            location = ""
            problem_mark = getattr(exc, "problem_mark", None)
            if problem_mark is not None:
                location = f" at line {problem_mark.line + 1}, column {problem_mark.column + 1}"
            raise SecretsError(f"MCPG_SECRETS_FILE_PATH ({path!r}) is not valid YAML{location}") from exc
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            # Same as YAML above: include line/column only, never the
            # exception message — JSONDecodeError.msg can include the
            # offending character which may be inside a secret value.
            raise SecretsError(
                f"MCPG_SECRETS_FILE_PATH ({path!r}) is not valid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc

    if not isinstance(data, dict):
        raise SecretsError(f"MCPG_SECRETS_FILE_PATH ({path!r}) must contain a top-level object of name -> value pairs")

    overlay: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise SecretsError(f"MCPG_SECRETS_FILE_PATH ({path!r}) has a non-string key {key!r}")
        if value is None:
            continue
        # Coerce scalars (a YAML int/bool, or a JSON number) to str so
        # downstream code that expects string secrets is unaffected.
        if isinstance(value, (dict, list)):
            raise SecretsError(
                f"MCPG_SECRETS_FILE_PATH ({path!r}) value for {key!r} must be a scalar, not {type(value).__name__}"
            )
        overlay[key] = value if isinstance(value, str) else str(value)
    return overlay


# Per-provider cache cap. The cloud-secrets caches were unbounded
# ``dict[str, str | None]`` instances; an attacker (or buggy caller)
# hitting ``provider.get(unique_name_each_call)`` in a loop would
# grow them until OOM. Bounding at 1024 covers any reasonable
# deployment's working set (MCPG_* + vendor env vars + a few hundred
# tenanted lookups) while keeping the worst-case memory footprint
# small (string keys + small string values → kilobytes, not gigs).
_SECRET_CACHE_MAX_ENTRIES = 1024

# OrderedDict's ``move_to_end`` / ``popitem`` and the bounded-eviction
# loop are not atomic, so a sync ``provider.get`` called from a worker
# thread (e.g. via ``asyncio.to_thread`` — already used elsewhere in
# the codebase for sync SDK calls) could race with the asyncio loop's
# own get and corrupt the linked list. Per-cache mutation cost is a
# single Lock acquire / release, well under a microsecond, so this is
# pure upside (gemini review on #101).
_CACHE_LOCK = threading.Lock()


def _cache_get(cache: OrderedDict[str, str | None], name: str) -> tuple[str | None, bool]:
    """Return ``(value, present)`` for a name lookup.

    ``present=False`` distinguishes "name never cached" from "cached
    as None" (a name explicitly absent from the backend). On a hit
    the entry is moved to the back of the OrderedDict so the LRU
    eviction in :func:`_cache_put` drops cold entries first.
    """
    with _CACHE_LOCK:
        if name in cache:
            cache.move_to_end(name)
            return cache[name], True
        return None, False


def _cache_put(cache: OrderedDict[str, str | None], name: str, value: str | None) -> None:
    """Insert ``(name, value)`` with bounded-LRU semantics.

    Replaces any existing entry, marks it most-recently-used, then
    evicts the oldest entries until the size is within
    :data:`_SECRET_CACHE_MAX_ENTRIES`.
    """
    with _CACHE_LOCK:
        cache[name] = value
        cache.move_to_end(name)
        while len(cache) > _SECRET_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)


@dataclass
class VaultSecretsProvider:
    """Reads secrets from HashiCorp Vault's KV v2 backend.

    Each looked-up ``name`` is split on the last ``/`` so callers can
    address sub-paths (``MCPG_DATABASE_URL`` → ``<prefix>/MCPG_DATABASE_URL``
    and read the ``value`` field; ``foo/bar`` → ``<prefix>/foo`` and
    read the ``bar`` field). Anything not stored in Vault falls back
    to the process environment.

    The ``hvac`` client is cached on first use rather than created at
    construction time so the provider can be instantiated cheaply (and
    the SDK import only fires once a real read happens). Per-name
    results are memoised because each Vault round-trip is a network
    call, and config touches happen many times per startup.
    """

    addr: str
    # The Vault token IS the credential — keeping it in the default
    # repr would let any ``logging.exception(provider)``,
    # ``logger.debug(f"provider={provider}")``, pytest assertion, or
    # ``Settings.model_dump()`` (when this provider is reachable from
    # Settings) write the root token into a log or a CI artefact.
    # repr=False is the surgical fix; the field stays accessible as
    # ``provider.token`` for code that legitimately needs it.
    token: str = field(repr=False)
    env: Mapping[str, str] = field(repr=False)
    namespace: str | None = None
    path_prefix: str = "secret/mcpg"
    _client: Any = field(default=None, init=False, repr=False, compare=False)
    _cache: OrderedDict[str, str | None] = field(default_factory=OrderedDict, init=False, repr=False, compare=False)

    def get(self, name: str) -> str | None:
        cached, present = _cache_get(self._cache, name)
        if present:
            # Stored ``None`` = "name is not in Vault"; fall through
            # to env so vendor-conventional env vars still resolve.
            return cached if cached is not None else self.env.get(name)

        value = self._fetch(name)
        _cache_put(self._cache, name, value)
        if value is not None:
            return value
        return self.env.get(name)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import hvac
        except ImportError as exc:
            raise SecretsError("MCPG_SECRETS_BACKEND=vault requires the `mcpg[vault]` extra (install `hvac`)") from exc
        # Do NOT call ``client.is_authenticated()`` here. The real
        # token-validation signal — Forbidden / Unauthorized on a
        # read — is surfaced explicitly in ``_fetch`` below as a
        # SecretsError, which gives the operator a clear "rotate the
        # token" message. ``is_authenticated`` adds a round-trip at
        # construction time AND was reviewed (PR #65) as misleading
        # because some hvac versions implement it as a local bool
        # check.
        self._client = hvac.Client(url=self.addr, token=self.token, namespace=self.namespace)
        return self._client

    def _fetch(self, name: str) -> str | None:
        # Split on the last ``/`` so callers can store grouped secrets
        # at a single Vault path (the common pattern). When no ``/``
        # appears, the ``name`` IS the path and we read the ``value``
        # field by convention.
        if "/" in name:
            path, field_name = name.rsplit("/", 1)
        else:
            path, field_name = name, "value"
        client = self._ensure_client()
        full_path = f"{self.path_prefix.rstrip('/')}/{path}"
        try:
            response = client.secrets.kv.v2.read_secret_version(path=full_path)
        except Exception as exc:
            # Distinguish "secret not present at this path" (fall
            # through to env) from "Vault refused the token" (raise so
            # the operator sees the real problem instead of silently
            # falling back to env and getting a different config-error
            # ten seconds later). The hvac exception tree is
            # deliberately imported inside the except so the lazy-SDK
            # contract still holds at module import time.
            try:
                from hvac import exceptions as hvac_exc
            except ImportError:
                return None
            if isinstance(exc, (hvac_exc.Forbidden, hvac_exc.Unauthorized)):
                raise SecretsError(
                    f"Vault denied access to {full_path!r} — check MCPG_VAULT_TOKEN and policy: {exc}"
                ) from exc
            return None
        if not response:
            return None
        data = response.get("data", {}).get("data", {}) if isinstance(response, dict) else {}
        value = data.get(field_name)
        return str(value) if value is not None else None


@dataclass
class AWSSecretsProvider:
    """Reads secrets from AWS Secrets Manager via boto3.

    The optional ``prefix`` is prepended to the requested name on each
    lookup so a deployment can scope its MCPg secrets under a single
    path. Values can be either ``SecretString`` (returned verbatim)
    or a JSON object — when the latter, the field whose key matches
    the looked-up name (without prefix) is returned.

    Authentication uses boto3's standard chain (env / IAM role /
    config file); we never inject credentials into the SDK client. As
    with the Vault provider, results are memoised in-process and the
    SDK is lazily imported so the provider can be instantiated when
    boto3 isn't installed yet (the failure surfaces on first lookup).
    """

    region: str | None
    # No long-lived credential lives on the AWS provider (auth is via
    # the IAM env / role chain inside boto3), but the env mapping
    # itself can hold AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN — same
    # repr=False rationale as the other providers.
    env: Mapping[str, str] = field(repr=False)
    prefix: str = ""
    _client: Any = field(default=None, init=False, repr=False, compare=False)
    _cache: OrderedDict[str, str | None] = field(default_factory=OrderedDict, init=False, repr=False, compare=False)

    def get(self, name: str) -> str | None:
        cached, present = _cache_get(self._cache, name)
        if present:
            return cached if cached is not None else self.env.get(name)
        value = self._fetch(name)
        _cache_put(self._cache, name, value)
        if value is not None:
            return value
        return self.env.get(name)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:
            raise SecretsError("MCPG_SECRETS_BACKEND=aws requires the `mcpg[aws]` extra (install `boto3`)") from exc
        kwargs: dict[str, object] = {}
        if self.region:
            kwargs["region_name"] = self.region
        self._client = boto3.client("secretsmanager", **kwargs)
        return self._client

    def _fetch(self, name: str) -> str | None:
        client = self._ensure_client()
        full_name = f"{self.prefix}{name}" if self.prefix else name
        try:
            response = client.get_secret_value(SecretId=full_name)
        except Exception as exc:
            # Distinguish "secret not present" (fall through to env)
            # from "AWS refused the request" (raise so the operator
            # sees the auth problem instead of silently falling back).
            # ResourceNotFound stays None; access / signature errors
            # become SecretsError. ClientError is the botocore base
            # class; ``error_code`` lives under ``response.Error.Code``.
            try:
                from botocore import exceptions as boto_exc
            except ImportError:
                return None
            if isinstance(exc, boto_exc.ClientError):
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code in {
                    "AccessDeniedException",
                    "UnrecognizedClientException",
                    "InvalidSignatureException",
                    "ExpiredTokenException",
                }:
                    raise SecretsError(
                        f"AWS Secrets Manager denied access to {full_name!r} (error code {code!r}): {exc}"
                    ) from exc
            return None
        if not isinstance(response, dict):
            return None
        raw = response.get("SecretString")
        if raw is None:
            return None
        # SecretString is usually JSON for multi-value secrets and a
        # bare string for single-value secrets. Try to parse: if
        # parsing succeeds and the dict has a field matching the
        # looked-up name, return that; otherwise fall back to the raw
        # string so service-account JSON / OAuth-blob payloads
        # (no key matching the secret name) still surface as values.
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return str(raw)
        if isinstance(parsed, dict):
            if name in parsed:
                value = parsed[name]
                return str(value) if value is not None else None
            return str(raw)
        return str(raw)


@dataclass
class GCPSecretsProvider:
    """Reads secrets from Google Cloud Secret Manager.

    Each looked-up name resolves to ``projects/{project_id}/secrets/{prefix+name}/versions/latest``;
    the payload's UTF-8 string body is returned. As with Vault and
    AWS, results are memoised and the SDK is lazily imported.
    """

    project_id: str
    env: Mapping[str, str] = field(repr=False)
    prefix: str = ""
    _client: Any = field(default=None, init=False, repr=False, compare=False)
    _cache: OrderedDict[str, str | None] = field(default_factory=OrderedDict, init=False, repr=False, compare=False)

    def get(self, name: str) -> str | None:
        cached, present = _cache_get(self._cache, name)
        if present:
            return cached if cached is not None else self.env.get(name)
        value = self._fetch(name)
        _cache_put(self._cache, name, value)
        if value is not None:
            return value
        return self.env.get(name)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import secretmanager
        except ImportError as exc:
            raise SecretsError(
                "MCPG_SECRETS_BACKEND=gcp requires the `mcpg[gcp]` extra (install `google-cloud-secret-manager`)"
            ) from exc
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _fetch(self, name: str) -> str | None:
        client = self._ensure_client()
        full_name = f"{self.prefix}{name}" if self.prefix else name
        resource = f"projects/{self.project_id}/secrets/{full_name}/versions/latest"
        try:
            response = client.access_secret_version(name=resource)
        except Exception as exc:
            # Distinguish NotFound (fall through to env) from
            # auth / permission failures (raise so the operator sees
            # the real problem). google.api_core's exception classes
            # are imported lazily to keep the lazy-SDK contract.
            try:
                from google.api_core import exceptions as google_exc
            except ImportError:
                return None
            if isinstance(exc, (google_exc.PermissionDenied, google_exc.Unauthenticated)):
                raise SecretsError(
                    f"GCP Secret Manager denied access to {resource!r} — check the service account's roles: {exc}"
                ) from exc
            return None
        payload = getattr(response, "payload", None)
        if payload is None:
            return None
        data = getattr(payload, "data", None)
        if data is None:
            return None
        try:
            decoded = data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return None
        return str(decoded)


def build_secrets_provider(env: Mapping[str, str]) -> tuple[SecretsProvider, str]:
    """Construct the secrets provider selected by ``MCPG_SECRETS_BACKEND``.

    Defaults to the ``env`` backend (current behaviour). ``file`` loads
    ``MCPG_SECRETS_FILE_PATH`` once at startup.

    Returns:
        ``(provider, backend_name)`` — the normalised, validated backend
        name comes back alongside the provider so ``load_settings``
        doesn't have to re-parse ``MCPG_SECRETS_BACKEND``.

    Raises:
        SecretsError: For an unknown backend, a missing file path, or an
            unreadable / malformed secrets file.
    """
    backend = (env.get("MCPG_SECRETS_BACKEND") or "env").strip().lower()
    if backend not in _SUPPORTED_BACKENDS:
        raise SecretsError(f"MCPG_SECRETS_BACKEND must be one of {sorted(_SUPPORTED_BACKENDS)} (got {backend!r})")

    if backend == "file":
        path = (env.get("MCPG_SECRETS_FILE_PATH") or "").strip()
        if not path:
            raise SecretsError("MCPG_SECRETS_BACKEND=file requires MCPG_SECRETS_FILE_PATH")
        return FileSecretsProvider(overlay=_load_overlay(path), env=env), backend

    if backend == "vault":
        addr = (env.get("MCPG_VAULT_ADDR") or "").strip()
        token = (env.get("MCPG_VAULT_TOKEN") or "").strip()
        if not addr:
            raise SecretsError("MCPG_SECRETS_BACKEND=vault requires MCPG_VAULT_ADDR")
        if not token:
            raise SecretsError("MCPG_SECRETS_BACKEND=vault requires MCPG_VAULT_TOKEN")
        namespace = (env.get("MCPG_VAULT_NAMESPACE") or "").strip() or None
        path_prefix = (env.get("MCPG_VAULT_PATH_PREFIX") or "secret/mcpg").strip() or "secret/mcpg"
        return (
            VaultSecretsProvider(
                addr=addr,
                token=token,
                env=env,
                namespace=namespace,
                path_prefix=path_prefix,
            ),
            backend,
        )

    if backend == "aws":
        region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None
        prefix = (env.get("MCPG_AWS_SECRETS_PREFIX") or "").strip()
        return AWSSecretsProvider(region=region, env=env, prefix=prefix), backend

    if backend == "gcp":
        project_id = (env.get("MCPG_GCP_PROJECT_ID") or "").strip()
        if not project_id:
            raise SecretsError("MCPG_SECRETS_BACKEND=gcp requires MCPG_GCP_PROJECT_ID")
        prefix = (env.get("MCPG_GCP_SECRETS_PREFIX") or "").strip()
        return GCPSecretsProvider(project_id=project_id, env=env, prefix=prefix), backend

    return EnvSecretsProvider(env=env), backend
