"""Pluggable secrets backend.

By default MCPg reads every secret straight from the process
environment (``MCPG_SECRETS_BACKEND=env``) — the historical behaviour,
zero new dependencies. Deployments that keep credentials in a mounted
file can set ``MCPG_SECRETS_BACKEND=file`` + ``MCPG_SECRETS_FILE_PATH``
to load a flat ``name -> value`` map of JSON (always) or YAML (when
PyYAML is importable).

A provider only supplies *secret values*; non-secret configuration
still comes from the environment. The ``file`` provider overlays the
file on top of the environment — a name present in the file wins, and
anything absent falls back to the env var — so partial files and the
vendor-conventional API-key env vars keep working.

Cloud backends (Vault / AWS Secrets Manager / GCP Secret Manager) are
designed for but not yet implemented; they will register here behind
optional extras and be selected by the same ``MCPG_SECRETS_BACKEND``
switch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_SUPPORTED_BACKENDS = frozenset({"env", "file"})


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

    env: Mapping[str, str]

    def get(self, name: str) -> str | None:
        return self.env.get(name)


@dataclass(frozen=True, slots=True)
class FileSecretsProvider:
    """Reads secrets from a file overlay, falling back to the environment.

    ``overlay`` is the flat ``name -> value`` map loaded from the
    configured file; a name present there wins, everything else defers
    to ``env`` so non-secret config and unlisted vendor keys still work.
    """

    overlay: Mapping[str, str]
    env: Mapping[str, str]

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

    return EnvSecretsProvider(env=env), backend
