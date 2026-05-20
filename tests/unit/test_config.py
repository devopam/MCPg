"""Tests for the env-driven configuration loader."""

import pytest

from mcpg.config import AccessMode, ConfigError, Transport, load_settings

_DB_URL = "postgresql://user:secret@localhost:5432/app"


def test_loads_database_url_and_applies_safe_defaults() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})

    assert settings.database_url == _DB_URL
    # Read-only is the safe default (PLAN.md principle: read-only by default).
    assert settings.access_mode is AccessMode.READ_ONLY
    assert settings.transport is Transport.STDIO
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 8000
    assert settings.log_level == "INFO"


def test_missing_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_DATABASE_URL"):
        load_settings({})


def test_blank_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_DATABASE_URL"):
        load_settings({"MCPG_DATABASE_URL": "   "})


def test_access_mode_is_parsed_case_insensitively() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ACCESS_MODE": "UnRestricted"})
    assert settings.access_mode is AccessMode.UNRESTRICTED


def test_invalid_access_mode_raises_with_valid_options() -> None:
    with pytest.raises(ConfigError, match="read-only"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ACCESS_MODE": "bogus"})


def test_transport_is_parsed() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_TRANSPORT": "streamable-http"})
    assert settings.transport is Transport.STREAMABLE_HTTP


def test_invalid_transport_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_TRANSPORT"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_TRANSPORT": "carrier-pigeon"})


def test_http_port_is_parsed_as_int() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_HTTP_PORT": "5000"})
    assert settings.http_port == 5000


def test_non_numeric_http_port_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_HTTP_PORT"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_HTTP_PORT": "abc"})


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_out_of_range_http_port_raises(port: str) -> None:
    with pytest.raises(ConfigError, match="MCPG_HTTP_PORT"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_HTTP_PORT": port})


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_LOG_LEVEL"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LOG_LEVEL": "verbose"})


def test_log_level_is_normalised_to_upper_case() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LOG_LEVEL": "debug"})
    assert settings.log_level == "DEBUG"


def test_settings_is_immutable() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    with pytest.raises(AttributeError):
        settings.http_port = 1234  # type: ignore[misc]


def test_allow_ddl_defaults_to_false() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).allow_ddl is False


def test_allow_ddl_is_parsed_from_common_boolean_spellings() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOW_DDL": "true"}).allow_ddl is True
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOW_DDL": "OFF"}).allow_ddl is False


def test_invalid_allow_ddl_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_ALLOW_DDL"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOW_DDL": "maybe"})


def test_repr_does_not_leak_the_password() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    rendered = repr(settings)
    assert "secret" not in rendered
    assert "****" in rendered
