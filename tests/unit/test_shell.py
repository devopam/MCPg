"""Tests for the subprocess execution policy (ADR-0004)."""

from typing import Any

import pytest

from mcpg.shell import (
    ShellError,
    SubprocessLimits,
    SubprocessResult,
    _filter_env,
    _make_preexec_fn,
    _redact_env,
    _resolve_binary,
    run_pg_binary,
)

# --- _resolve_binary ------------------------------------------------------


def test_resolve_binary_rejects_anything_off_the_allowlist() -> None:
    with pytest.raises(ShellError, match="not on the allowlist"):
        _resolve_binary("rm")


def test_resolve_binary_rejects_a_missing_allowlisted_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Monkeypatch shutil.which to simulate the binary not being on PATH.
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda _: None)
    with pytest.raises(ShellError, match="not found on PATH"):
        _resolve_binary("pg_dump")


def test_resolve_binary_returns_the_resolved_path_for_an_allowlisted_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda name: f"/usr/bin/{name}")
    assert _resolve_binary("pg_dump") == "/usr/bin/pg_dump"


# --- _redact_env / _filter_env -------------------------------------------


def test_redact_env_masks_pgpassword_but_preserves_other_vars() -> None:
    redacted = _redact_env({"PGHOST": "localhost", "PGUSER": "u", "PGPASSWORD": "hunter2"})
    assert redacted["PGHOST"] == "localhost"
    assert redacted["PGUSER"] == "u"
    assert redacted["PGPASSWORD"] == "****"


def test_filter_env_drops_non_allowlisted_keys() -> None:
    out = _filter_env({"PGHOST": "localhost", "PGPASSWORD": "x", "PYTHONPATH": "/danger", "LD_PRELOAD": "/evil.so"})
    assert "PGHOST" in out and "PGPASSWORD" in out
    assert "PYTHONPATH" not in out
    assert "LD_PRELOAD" not in out


def test_filter_env_always_includes_path_so_binaries_resolve() -> None:
    out = _filter_env({})
    assert "PATH" in out


# --- run_pg_binary --------------------------------------------------------


class _FakeStream:
    """Async stdout/stderr stand-in that yields the supplied chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int = -1) -> bytes:
        # When _size > 0 (run_pg_binary's stdout drain), hand the next
        # chunk; when -1 or omitted (stderr full-read), concatenate.
        if _size == -1 or _size is None:
            joined = b"".join(self._chunks)
            self._chunks.clear()
            return joined
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: list[bytes],
        stderr: bytes,
        returncode: int,
    ) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream([stderr])
        self.stdin = None
        self.returncode = returncode
        self._killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self._killed = True


def _patch_exec(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> dict[str, Any]:
    """Patch asyncio.create_subprocess_exec; return a recorder dict."""
    record: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        record["args"] = args
        record["kwargs"] = kwargs
        return process

    monkeypatch.setattr("mcpg.shell.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("mcpg.shell.asyncio.create_subprocess_exec", fake_exec)
    return record


async def test_run_pg_binary_captures_stdout_and_stderr_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=[b"line1\n", b"line2\n"], stderr=b"warning", returncode=0)
    record = _patch_exec(monkeypatch, process)

    result = await run_pg_binary("pg_dump", "--version", timeout_sec=10, max_output_bytes=1024)

    assert isinstance(result, SubprocessResult)
    assert result.exit_code == 0
    assert result.stdout == b"line1\nline2\n"
    assert result.stderr == b"warning"
    assert result.output_bytes == 12
    assert result.output_truncated is False
    assert result.timed_out is False
    # Argv passed through unchanged; first positional is the resolved binary path.
    assert record["args"] == ("/usr/bin/pg_dump", "--version")


async def test_run_pg_binary_truncates_stdout_at_max_output_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 5 chunks of 100 bytes each = 500 bytes; cap at 250 keeps the
    # first 2.5 chunks and flags truncation.
    chunks = [b"x" * 100 for _ in range(5)]
    process = _FakeProcess(stdout=chunks, stderr=b"", returncode=0)
    _patch_exec(monkeypatch, process)

    result = await run_pg_binary("pg_dump", timeout_sec=10, max_output_bytes=250)

    assert result.output_truncated is True
    assert len(result.stdout) == 250
    # output_bytes counts everything the child actually wrote, not just
    # what we kept, so callers can see the real volume.
    assert result.output_bytes == 500


async def test_run_pg_binary_redacts_pgpassword_in_the_env_returned_with_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=[b""], stderr=b"", returncode=0)
    _patch_exec(monkeypatch, process)

    env = {"PGHOST": "localhost", "PGPASSWORD": "hunter2"}
    result = await run_pg_binary("pg_dump", env=env, timeout_sec=10, max_output_bytes=1024)

    assert result.env_redacted["PGHOST"] == "localhost"
    assert result.env_redacted["PGPASSWORD"] == "****"


async def test_run_pg_binary_passes_only_allowlisted_env_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=[b""], stderr=b"", returncode=0)
    record = _patch_exec(monkeypatch, process)

    env = {"PGUSER": "u", "PGPASSWORD": "x", "PYTHONPATH": "/danger"}
    await run_pg_binary("pg_dump", env=env, timeout_sec=10, max_output_bytes=1024)

    child_env = record["kwargs"]["env"]
    assert "PGUSER" in child_env and "PGPASSWORD" in child_env
    assert "PYTHONPATH" not in child_env


async def test_run_pg_binary_raises_when_binary_is_not_allowlisted() -> None:
    with pytest.raises(ShellError, match="not on the allowlist"):
        await run_pg_binary("rm", "-rf", "/", timeout_sec=1, max_output_bytes=1)


async def test_run_pg_binary_raises_when_binary_missing_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda _: None)
    with pytest.raises(ShellError, match="not found on PATH"):
        await run_pg_binary("pg_dump", timeout_sec=1, max_output_bytes=1)


async def test_run_pg_binary_flags_timeout_and_kills_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake process that never finishes — wait() blocks forever.
    class _HangingProcess(_FakeProcess):
        async def wait(self) -> int:
            import asyncio as _asyncio

            await _asyncio.sleep(10)  # always longer than the test timeout
            return 0

    process = _HangingProcess(stdout=[], stderr=b"", returncode=-9)
    _patch_exec(monkeypatch, process)

    result = await run_pg_binary("pg_dump", timeout_sec=0, max_output_bytes=1024)
    # timeout_sec=0 triggers asyncio.timeout(0) which fires immediately.
    assert result.timed_out is True
    assert process._killed is True


class _BrokenStdin:
    """A stdin pipe stand-in whose write/drain/close raise BrokenPipeError."""

    def write(self, _data: bytes) -> None:
        raise BrokenPipeError("child closed stdin early")

    async def drain(self) -> None:
        raise BrokenPipeError("child closed stdin early")

    def close(self) -> None:
        raise BrokenPipeError("child closed stdin early")

    async def wait_closed(self) -> None:
        raise BrokenPipeError("child closed stdin early")


async def test_run_pg_binary_surfaces_exit_code_when_child_closes_stdin_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulates `psql` exiting on a SQL error before the parent finishes
    # writing the dump. The previous implementation would propagate
    # BrokenPipeError through asyncio.gather and lose the exit code +
    # stderr. After the fix, the broken pipe is swallowed and the real
    # exit code and stderr surface in the result.
    process = _FakeProcess(stdout=[], stderr=b"ERROR: syntax", returncode=3)
    process.stdin = _BrokenStdin()  # type: ignore[assignment]
    _patch_exec(monkeypatch, process)

    result = await run_pg_binary(
        "psql",
        "--file=-",
        timeout_sec=10,
        max_output_bytes=1024,
        stdin=b"BAD SQL HERE;",
    )

    # No exception bubbled up — the agent gets the real diagnostic
    # instead of an opaque BrokenPipeError.
    assert result.exit_code == 3
    assert result.stderr == b"ERROR: syntax"
    assert result.timed_out is False


# --- subprocess hardening: binary-path allowlist --------------------------


def test_resolve_binary_accepts_a_path_inside_the_allowlisted_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda name: f"/usr/bin/{name}")
    resolved = _resolve_binary("pg_dump", bin_allowlist=("/usr/bin",))
    assert resolved == "/usr/bin/pg_dump"


def test_resolve_binary_rejects_a_path_outside_the_allowlisted_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # A PATH shim resolving to /tmp/evil must be rejected when the
    # operator pins the binary directory.
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda name: f"/tmp/evil/{name}")
    with pytest.raises(ShellError, match="outside MCPG_SUBPROCESS_BIN_ALLOWLIST"):
        _resolve_binary("pg_dump", bin_allowlist=("/usr/bin", "/usr/local/bin"))


# --- subprocess hardening: rlimit preexec_fn ------------------------------


def test_make_preexec_fn_is_none_when_no_limits_requested() -> None:
    assert _make_preexec_fn(SubprocessLimits()) is None


def test_make_preexec_fn_applies_cpu_and_memory_rlimits(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcpg.shell as shell_mod

    if shell_mod.resource is None:  # pragma: no cover - non-POSIX
        pytest.skip("resource module not available on this platform")

    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(shell_mod.resource, "setrlimit", lambda which, limit: calls.append((which, limit)))

    fn = _make_preexec_fn(SubprocessLimits(cpu_seconds=5, memory_mb=128))
    assert fn is not None
    fn()  # would run in the forked child; here it just records

    whichs = {which for which, _ in calls}
    assert shell_mod.resource.RLIMIT_CPU in whichs
    assert shell_mod.resource.RLIMIT_AS in whichs
    # Memory limit is converted to bytes.
    as_limit = next(limit for which, limit in calls if which == shell_mod.resource.RLIMIT_AS)
    assert as_limit == (128 * 1024 * 1024, 128 * 1024 * 1024)


# --- subprocess hardening: run_pg_binary spawn wiring ---------------------


async def test_run_pg_binary_spawns_in_a_temp_cwd_with_no_preexec_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stdout=[b"ok\n"], stderr=b"", returncode=0)
    record = _patch_exec(monkeypatch, process)

    await run_pg_binary("pg_dump", "--version", timeout_sec=10, max_output_bytes=1024)

    # A throwaway working directory is always passed; no rlimit preexec
    # unless limits are configured.
    assert record["kwargs"]["cwd"].startswith("/")
    assert record["kwargs"]["preexec_fn"] is None


async def test_run_pg_binary_passes_a_preexec_fn_when_limits_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcpg.shell as shell_mod

    if shell_mod.resource is None:  # pragma: no cover - non-POSIX
        pytest.skip("resource module not available on this platform")

    process = _FakeProcess(stdout=[b"ok\n"], stderr=b"", returncode=0)
    record = _patch_exec(monkeypatch, process)

    await run_pg_binary(
        "pg_dump",
        "--version",
        timeout_sec=10,
        max_output_bytes=1024,
        limits=SubprocessLimits(cpu_seconds=5, memory_mb=64),
    )

    assert callable(record["kwargs"]["preexec_fn"])


async def test_run_pg_binary_enforces_the_bin_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcpg.shell.shutil.which", lambda name: f"/opt/sneaky/{name}")
    with pytest.raises(ShellError, match="outside MCPG_SUBPROCESS_BIN_ALLOWLIST"):
        await run_pg_binary(
            "pg_dump",
            "--version",
            timeout_sec=10,
            max_output_bytes=1024,
            limits=SubprocessLimits(bin_allowlist=("/usr/bin",)),
        )
