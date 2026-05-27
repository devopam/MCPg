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


def test_allow_listen_defaults_to_false_and_parses_booleans() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).allow_listen is False
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOW_LISTEN": "true"}).allow_listen is True
    with pytest.raises(ConfigError, match="MCPG_ALLOW_LISTEN"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOW_LISTEN": "maybe"})


def test_listen_queue_max_defaults_and_parses() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).listen_queue_max == 1000
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LISTEN_QUEUE_MAX": "50"}).listen_queue_max == 50
    with pytest.raises(ConfigError, match="MCPG_LISTEN_QUEUE_MAX"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LISTEN_QUEUE_MAX": "0"})


def test_pool_sizes_default_to_one_and_five() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.pool_min_size == 1
    assert settings.pool_max_size == 5


def test_pool_sizes_are_parsed_from_the_environment() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_POOL_MIN_SIZE": "3", "MCPG_POOL_MAX_SIZE": "20"})
    assert settings.pool_min_size == 3
    assert settings.pool_max_size == 20


def test_non_numeric_pool_size_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_POOL_MAX_SIZE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_POOL_MAX_SIZE": "lots"})


def test_pool_size_below_one_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_POOL_MIN_SIZE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_POOL_MIN_SIZE": "0"})


def test_pool_max_below_min_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_POOL_MAX_SIZE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_POOL_MIN_SIZE": "10", "MCPG_POOL_MAX_SIZE": "5"})


def test_repr_does_not_leak_the_password() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    rendered = repr(settings)
    assert "secret" not in rendered
    assert "****" in rendered


# --- multi-tenancy: MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES (Phase 1.4) ---


def test_default_role_defaults_to_none_and_parses_when_set() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.default_role is None

    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_DEFAULT_ROLE": "app_reader"})
    assert settings.default_role == "app_reader"


def test_default_role_rejects_unsafe_identifiers() -> None:
    with pytest.raises(ConfigError, match="MCPG_DEFAULT_ROLE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_DEFAULT_ROLE": '"; DROP USER alice'})


def test_default_role_rejects_blank_string() -> None:
    with pytest.raises(ConfigError, match="MCPG_DEFAULT_ROLE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_DEFAULT_ROLE": "   "})


def test_allowed_roles_defaults_to_empty_tuple_and_parses_comma_list() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.allowed_roles == ()

    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOWED_ROLES": "tenant_a, tenant_b , tenant_c"})
    assert settings.allowed_roles == ("tenant_a", "tenant_b", "tenant_c")


def test_allowed_roles_rejects_unsafe_identifiers_in_the_list() -> None:
    with pytest.raises(ConfigError, match="MCPG_ALLOWED_ROLES"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_ALLOWED_ROLES": "tenant_a, bad-name"})


def test_default_role_must_appear_in_allowed_roles_when_both_set() -> None:
    with pytest.raises(ConfigError, match="MCPG_DEFAULT_ROLE"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_DEFAULT_ROLE": "tenant_z",
                "MCPG_ALLOWED_ROLES": "tenant_a,tenant_b",
            }
        )


# --- NL→SQL provider config (Phase 10.2) ---------------------------------


def test_nl2sql_defaults_to_unset_and_zero_overhead() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.nl2sql_provider is None
    assert settings.nl2sql_api_key is None
    assert settings.nl2sql_model is None
    assert settings.nl2sql_max_tokens == 2048


def test_nl2sql_provider_rejects_unknown_vendor() -> None:
    with pytest.raises(ConfigError, match="MCPG_NL2SQL_PROVIDER"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_PROVIDER": "perplexity",
                "MCPG_NL2SQL_API_KEY": "k",
            }
        )


def test_nl2sql_provider_requires_an_api_key_somewhere() -> None:
    with pytest.raises(ConfigError, match="no API key found"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_PROVIDER": "anthropic",
            }
        )


def test_nl2sql_explicit_api_key_wins_over_vendor_fallback() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "anthropic",
            "MCPG_NL2SQL_API_KEY": "explicit-key",
            "ANTHROPIC_API_KEY": "vendor-fallback",
        }
    )
    assert settings.nl2sql_api_key == "explicit-key"


def test_nl2sql_falls_back_to_anthropic_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "fallback",
        }
    )
    assert settings.nl2sql_api_key == "fallback"


def test_nl2sql_falls_back_to_openai_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "openai",
            "OPENAI_API_KEY": "fallback",
        }
    )
    assert settings.nl2sql_api_key == "fallback"


def test_nl2sql_falls_back_to_gemini_or_google_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "gemini",
            "GOOGLE_API_KEY": "google-fallback",
        }
    )
    assert settings.nl2sql_api_key == "google-fallback"

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-fallback",
            # GOOGLE_API_KEY also set — GEMINI wins because it's checked first.
            "GOOGLE_API_KEY": "google-fallback",
        }
    )
    assert settings.nl2sql_api_key == "gemini-fallback"


def test_nl2sql_rejects_max_tokens_above_hard_cap() -> None:
    with pytest.raises(ConfigError, match="hard cap"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_PROVIDER": "anthropic",
                "MCPG_NL2SQL_API_KEY": "k",
                "MCPG_NL2SQL_MAX_TOKENS": "100000",
            }
        )


def test_nl2sql_api_key_never_appears_in_repr() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "anthropic",
            "MCPG_NL2SQL_API_KEY": "sk-not-a-real-key-secret",
        }
    )
    rendered = repr(settings)
    assert "sk-not-a-real-key-secret" not in rendered
    assert "nl2sql_api_key='set'" in rendered


# --- replica routing (Phase 1.6) -----------------------------------------


def test_replica_urls_defaults_to_empty_tuple() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).replica_urls == ()


def test_replica_urls_parses_comma_separated_list() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_REPLICA_URLS": ("postgresql://u:p@replica-1/db, postgresql://u:p@replica-2/db"),
        }
    )
    assert settings.replica_urls == (
        "postgresql://u:p@replica-1/db",
        "postgresql://u:p@replica-2/db",
    )


def test_blank_replica_urls_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_REPLICA_URLS"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_REPLICA_URLS": "  ,  "})


def test_replica_repr_obfuscates_passwords() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_REPLICA_URLS": "postgresql://u:supersecret@replica/db",
        }
    )
    rendered = repr(settings)
    assert "supersecret" not in rendered
