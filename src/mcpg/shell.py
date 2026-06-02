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
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

try:  # POSIX-only; absent on Windows. Guarded so the import never breaks startup.
    import resource
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    resource = None  # type: ignore[assignment]

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
class SubprocessLimits:
    """Deployment-wide hardening policy for spawned PG binaries.

    All fields default to "no extra restriction" so the policy is
    opt-in. Populated from ``MCPG_SUBPROCESS_*`` settings and threaded
    into :func:`run_pg_binary`.

    Attributes:
        bin_allowlist: Absolute directory paths the resolved binary
            MUST live under. Empty means "trust PATH resolution" (the
            historical behaviour). When set, a binary resolved to a
            directory outside the allowlist is rejected — this defeats
            a malicious PATH shim of ``pg_dump`` / ``psql``.
        cpu_seconds: Per-process CPU-time rlimit (``RLIMIT_CPU``).
            ``None`` leaves the inherited limit untouched.
        memory_mb: Per-process address-space rlimit (``RLIMIT_AS``),
            in mebibytes. ``None`` leaves it untouched.
    """

    bin_allowlist: tuple[str, ...] = ()
    cpu_seconds: int | None = None
    memory_mb: int | None = None


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


def _resolve_binary(name: str, bin_allowlist: tuple[str, ...] = ()) -> str:
    """Look up ``name`` on PATH and return the absolute path, or raise.

    When ``bin_allowlist`` is non-empty, the resolved absolute path must
    live directly under one of the allowlisted directories; otherwise a
    PATH shim of ``pg_dump`` / ``psql`` is rejected before it can run.
    """
    if name not in _ALLOWED_BINARIES:
        raise ShellError(f"binary {name!r} is not on the allowlist (expected one of {sorted(_ALLOWED_BINARIES)})")
    resolved = shutil.which(name)
    if resolved is None:
        raise ShellError(f"binary {name!r} not found on PATH; install it on the server")
    if bin_allowlist:
        # Compare the *directory* PATH resolved the binary into — not the
        # realpath of the file, since distro packages legitimately symlink
        # e.g. /usr/bin/pg_dump -> pg_wrapper outside the bin dir. We only
        # normalise the directory for symlinks so a PATH shim in an
        # untrusted dir is still rejected.
        resolved_dir = os.path.realpath(os.path.dirname(resolved))
        allowed = {os.path.realpath(d) for d in bin_allowlist}
        if resolved_dir not in allowed:
            raise ShellError(
                f"binary {name!r} resolved to {resolved!r}, which is outside "
                f"MCPG_SUBPROCESS_BIN_ALLOWLIST ({sorted(bin_allowlist)})"
            )
    return resolved


def _make_preexec_fn(limits: SubprocessLimits) -> Callable[[], None] | None:
    """Build a child-side ``preexec_fn`` applying CPU / memory rlimits.

    Returns ``None`` when no rlimit is requested or the platform lacks
    the ``resource`` module (Windows), so the spawn path stays unchanged
    in those cases.
    """
    if resource is None:
        return None
    if limits.cpu_seconds is None and limits.memory_mb is None:
        return None

    cpu_seconds = limits.cpu_seconds
    memory_bytes = limits.memory_mb * 1024 * 1024 if limits.memory_mb is not None else None

    def _apply_limits() -> None:  # pragma: no cover - runs in the forked child
        if cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if memory_bytes is not None:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

    return _apply_limits


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
    limits: SubprocessLimits | None = None,
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
    limits = limits or SubprocessLimits()
    resolved = _resolve_binary(binary, limits.bin_allowlist)
    safe_env = _filter_env(env)
    redacted = _redact_env(safe_env)
    # Spawn in a throwaway working directory so a binary that writes
    # relative-path files (none of ours do today, but defence in depth)
    # can't litter the server's CWD. Cleaned up after the call.
    workdir = tempfile.mkdtemp(prefix="mcpg-pg-")
    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
            cwd=workdir,
            preexec_fn=_make_preexec_fn(limits),
        )
    except (OSError, FileNotFoundError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise ShellError(f"failed to spawn {binary!r}: {exc}") from exc

    timed_out = False
    truncated = False
    output_bytes_seen = 0
    stdout_chunks: list[bytes] = []
    # Initialise so an unexpected exception path doesn't leave this
    # name unbound when we build the SubprocessResult.
    stderr_bytes: bytes = b""

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

    async def _write_stdin() -> None:
        # The child may exit early on a SQL error (psql) or invalid
        # archive (pg_restore) while we're still writing. That closes
        # the pipe and raises BrokenPipeError / ConnectionResetError
        # here — if those propagate through asyncio.gather, we never
        # return the real exit code or stderr, and the agent loses
        # the diagnostic. Swallow them and let process.wait() +
        # stderr_task surface the actual failure cause.
        if stdin is None or process.stdin is None:
            return
        try:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            # Close UNCONDITIONALLY so the child always sees EOF even
            # when write/drain raised something other than a pipe error
            # (OSError on saturated pipe, RuntimeError from a closed
            # loop in tests). Without this, a non-pipe exception would
            # leave the child blocked reading stdin until the timeout.
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    try:
        async with asyncio.timeout(timeout_sec):
            stderr_task = asyncio.create_task(process.stderr.read())  # type: ignore[union-attr]
            # stdin is written concurrently with stdout/stderr draining
            # — pg_restore (and any consumer) only starts producing
            # stdout/stderr after it begins reading stdin, so the
            # earlier "write stdin after wait" ordering would deadlock.
            await asyncio.gather(_write_stdin(), _read_capped(), stderr_task, process.wait())
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

    shutil.rmtree(workdir, ignore_errors=True)
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
