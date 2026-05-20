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
