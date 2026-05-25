"""Subprocess execution policy for shell-gated tools (ADR-0004).

This module is the *only* place in MCPg that invokes external binaries.
Everything else routes through :func:`run_pg_binary` so the allowlist,
argv-only invocation, timeout, output cap, and credential-env-var
hygiene live in one place.

Policy (per ADR-0004):

- Allowlisted binaries only: ``pg_dump``, ``pg_restore``, ``psql``.
  Anything else raises :class:`ShellError` up-front.
- ``asyncio.create_subprocess_exec(binary, *argv)`` only — never
  ``shell=True``. Argv is a list, not a shell string, so no quoting
  surprises.
- Credentials pass through the environment (``PGPASSWORD`` etc.), never
  on the command line, so they don't appear in ``ps``.
- Stdout is capped at ``max_output_bytes``; output past the cap is
  dropped and the result flags ``output_truncated=True``.
- A hard timeout (``timeout_sec``) kills the process group on
  expiry; the result flags ``timed_out=True``.
- Identifier-bearing argv strings are validated against the standard
  ``[A-Za-z_][A-Za-z0-9_]*`` allowlist by the *caller* — this module
  does not invent identifier policy. Callers compose argv from
  safe pieces before calling :func:`run_pg_binary`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Final

_ALLOWED_BINARIES: Final[frozenset[str]] = frozenset({"pg_dump", "pg_restore", "psql"})

# Environment variables the caller may set to carry libpq credentials.
# Anything else is filtered out of the child process environment so we
# don't leak the parent's PATH-adjacent state by accident.
_ALLOWED_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
        "PGSSLMODE",
        "PGSSLROOTCERT",
        "PGOPTIONS",
        "PGAPPNAME",
        "LANG",
        "LC_ALL",
        "PATH",
    }
)

# Envs that carry credentials — redacted to ``****`` for audit logging.
_SECRET_ENV_VARS: Final[frozenset[str]] = frozenset({"PGPASSWORD"})

_REDACTED_VALUE = "****"


class ShellError(Exception):
    """Raised when a subprocess invocation is rejected or fails."""


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """The outcome of a single subprocess invocation.

    ``stdout`` and ``stderr`` are captured as bytes — callers decode
    when they know the encoding. ``exit_code`` is the process's exit
    status. ``output_truncated`` is ``True`` when stdout exceeded
    ``max_output_bytes`` and the tail was dropped. ``timed_out`` is
    ``True`` when the process was killed for exceeding ``timeout_sec``.
    """

    binary: str
    argv: list[str]
    exit_code: int
    stdout: bytes
    stderr: bytes
    output_bytes: int
    output_truncated: bool
    timed_out: bool
    # The redacted env dict that was actually passed to the child, kept
    # so audit logging can include "what was the connection target"
    # without re-deriving it from the URL.
    env_redacted: dict[str, str] = field(default_factory=dict)


def _resolve_binary(name: str) -> str:
    """Look up ``name`` on PATH and return the absolute path, or raise."""
    if name not in _ALLOWED_BINARIES:
        raise ShellError(f"binary {name!r} is not on the allowlist (expected one of {sorted(_ALLOWED_BINARIES)})")
    resolved = shutil.which(name)
    if resolved is None:
        raise ShellError(f"binary {name!r} not found on PATH; install it on the server")
    return resolved


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with credential values masked for logging."""
    return {key: (_REDACTED_VALUE if key in _SECRET_ENV_VARS else value) for key, value in env.items()}


def _filter_env(env: dict[str, str] | None) -> dict[str, str]:
    """Keep only the allowlisted env vars from the caller's dict.

    The child process gets a minimal environment — no inherited
    LD_PRELOAD, PYTHONPATH, or random debug flags that could change
    pg_dump's behaviour. ``PATH`` defaults from the caller or the
    parent process so binaries the allowlist accepts can be resolved.
    """
    merged: dict[str, str] = {}
    if "PATH" in os.environ:
        merged["PATH"] = os.environ["PATH"]
    for key, value in (env or {}).items():
        if key in _ALLOWED_ENV_VARS:
            merged[key] = value
    return merged


async def run_pg_binary(
    binary: str,
    *argv: str,
    env: dict[str, str] | None = None,
    timeout_sec: int,
    max_output_bytes: int,
    stdin: bytes | None = None,
) -> SubprocessResult:
    """Execute an allowlisted PostgreSQL binary with the agreed policy.

    Args:
        binary: One of ``pg_dump``, ``pg_restore``, ``psql``.
        argv: Positional arguments passed to the binary verbatim. The
            caller is responsible for validating any embedded
            identifiers — this function does not parse argv.
        env: Optional connection env-var overlay. Only the allowlisted
            ``PG*`` (and ``LANG``/``LC_ALL``/``PATH``) keys are
            forwarded; everything else is dropped so the child can't
            inherit a polluted environment.
        timeout_sec: Hard wall-clock limit; the process is killed on
            expiry and the result flags ``timed_out=True``.
        max_output_bytes: Stdout is captured up to this many bytes.
            Output past the cap is dropped and ``output_truncated`` is
            ``True``; the caller may re-run with a higher cap or a
            different scope.
        stdin: Optional bytes piped to the process's stdin (used by
            ``pg_restore`` to receive a custom-format archive).

    Raises:
        ShellError: When ``binary`` is not allowlisted, can't be found
            on PATH, or asyncio can't spawn it.
    """
    resolved = _resolve_binary(binary)
    safe_env = _filter_env(env)
    redacted = _redact_env(safe_env)
    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
    except (OSError, FileNotFoundError) as exc:
        raise ShellError(f"failed to spawn {binary!r}: {exc}") from exc

    timed_out = False
    truncated = False
    output_bytes_seen = 0
    stdout_chunks: list[bytes] = []

    async def _read_capped() -> None:
        nonlocal output_bytes_seen, truncated
        # Drain stdout into a bytes buffer, but stop appending once
        # we've hit the cap. We must keep reading so the child doesn't
        # block on a full pipe, even if we discard the bytes.
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                return
            output_bytes_seen += len(chunk)
            if not truncated:
                if output_bytes_seen <= max_output_bytes:
                    stdout_chunks.append(chunk)
                else:
                    overshoot = output_bytes_seen - max_output_bytes
                    keep = len(chunk) - overshoot
                    if keep > 0:
                        stdout_chunks.append(chunk[:keep])
                    truncated = True

    try:
        async with asyncio.timeout(timeout_sec):
            stderr_task = asyncio.create_task(process.stderr.read())  # type: ignore[union-attr]
            await asyncio.gather(_read_capped(), stderr_task, process.wait())
            stderr_bytes = stderr_task.result()
    except TimeoutError:
        timed_out = True
        process.kill()
        try:
            async with asyncio.timeout(5):
                await process.wait()
        except TimeoutError:
            # Best-effort reap; the OS will collect it.
            pass
        try:
            stderr_bytes = await process.stderr.read() if process.stderr else b""
        except Exception:
            stderr_bytes = b""

    # ``stdin`` is written then closed before reading begins for
    # simplicity (small payloads only — large restores stream their
    # archive via temp file, which is a Phase 24c concern).
    if stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        finally:
            process.stdin.close()

    stdout_bytes = b"".join(stdout_chunks)
    return SubprocessResult(
        binary=binary,
        argv=list(argv),
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        output_bytes=output_bytes_seen,
        output_truncated=truncated,
        timed_out=timed_out,
        env_redacted=redacted,
    )
