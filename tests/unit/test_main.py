"""Tests for the console entry point."""

import pytest

from mcpg import __main__
from mcpg.config import Settings


def test_main_returns_1_and_reports_when_config_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MCPG_DATABASE_URL", raising=False)

    exit_code = __main__.main()

    assert exit_code == 1
    assert "configuration error" in capsys.readouterr().err


def test_main_runs_the_server_with_loaded_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_DATABASE_URL", "postgresql://u:p@localhost/db")
    received: list[Settings] = []
    monkeypatch.setattr(__main__, "run", received.append)

    exit_code = __main__.main()

    assert exit_code == 0
    assert len(received) == 1
    assert received[0].database_url == "postgresql://u:p@localhost/db"


def test_demo_flag_seeds_and_prints_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcpg import demo

    monkeypatch.setenv("MCPG_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setattr("sys.argv", ["mcpg", "--demo"])
    seeded: list[str] = []

    async def fake_seed(dsn: str) -> demo.DemoSeedSummary:
        seeded.append(dsn)
        return demo.DemoSeedSummary(schema="mcpg_demo", row_counts={"orders": 3000}, vector_column_included=False)

    monkeypatch.setattr(demo, "seed_demo", fake_seed)

    exit_code = __main__.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert seeded == ["postgresql://u:p@localhost/db"]
    assert "Seeded the 'mcpg_demo' schema" in out
    assert "3000" in out
    assert "mcpg --demo-drop" in out


def test_demo_drop_flag_reports_the_drop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from mcpg import demo

    monkeypatch.setenv("MCPG_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setattr("sys.argv", ["mcpg", "--demo-drop"])

    async def fake_drop(dsn: str) -> demo.DemoDropSummary:
        return demo.DemoDropSummary(schema="mcpg_demo", dropped=True)

    monkeypatch.setattr(demo, "drop_demo", fake_drop)

    exit_code = __main__.main()

    assert exit_code == 0
    assert "Dropped the 'mcpg_demo' schema" in capsys.readouterr().out


def test_demo_error_exits_nonzero_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcpg import demo

    monkeypatch.setenv("MCPG_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setattr("sys.argv", ["mcpg", "--demo"])

    async def fake_seed(dsn: str) -> demo.DemoSeedSummary:
        raise demo.DemoError("schema 'mcpg_demo' already exists")

    monkeypatch.setattr(demo, "seed_demo", fake_seed)

    exit_code = __main__.main()

    assert exit_code == 1
    assert "demo error" in capsys.readouterr().err
