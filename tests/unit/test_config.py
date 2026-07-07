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


def test_log_format_defaults_to_text() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.log_format == "text"


def test_log_format_parses_json_case_insensitively() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LOG_FORMAT": "JSON"})
    assert settings.log_format == "json"


def test_invalid_log_format_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_LOG_FORMAT"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_LOG_FORMAT": "yaml"})


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
    # Use a distinctive password — "secret" would collide with the
    # legitimate ``secrets_backend`` repr field.
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://user:hunter2pw@localhost:5432/app"})
    rendered = repr(settings)
    assert "hunter2pw" not in rendered
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
    assert settings.nl2sql_api_keys == ()
    assert settings.nl2sql_model is None
    assert settings.nl2sql_max_tokens == 2048
    # NL→SQL audit persistence defaults: off, no backend forced,
    # 90-day retention, RLS on, no reader role.
    assert settings.nl2sql_audit_persist is False
    assert settings.nl2sql_audit_backend is None
    assert settings.nl2sql_audit_retention_days == 90
    assert settings.nl2sql_audit_chunk_interval == "1 day"
    assert settings.nl2sql_audit_compress_after == "7 days"
    assert settings.nl2sql_audit_rls is True
    assert settings.nl2sql_audit_reader_role is None


def test_nl2sql_audit_env_vars_round_trip_through_settings() -> None:
    """Verify each MCPG_NL2SQL_AUDIT_* knob lands in the right field."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_AUDIT_PERSIST": "true",
            "MCPG_NL2SQL_AUDIT_BACKEND": "native",
            "MCPG_NL2SQL_AUDIT_RETENTION_DAYS": "30",
            "MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL": "1 hour",
            "MCPG_NL2SQL_AUDIT_COMPRESS_AFTER": "2 days",
            "MCPG_NL2SQL_AUDIT_RLS": "false",
            "MCPG_NL2SQL_AUDIT_READER_ROLE": "analytics_ro",
        }
    )
    assert settings.nl2sql_audit_persist is True
    assert settings.nl2sql_audit_backend == "native"
    assert settings.nl2sql_audit_retention_days == 30
    assert settings.nl2sql_audit_chunk_interval == "1 hour"
    assert settings.nl2sql_audit_compress_after == "2 days"
    assert settings.nl2sql_audit_rls is False
    assert settings.nl2sql_audit_reader_role == "analytics_ro"


def test_nl2sql_audit_backend_rejects_unknown_choice() -> None:
    with pytest.raises(ConfigError, match="MCPG_NL2SQL_AUDIT_BACKEND"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_AUDIT_BACKEND": "mongodb",
            }
        )


@pytest.mark.parametrize(
    "var,bad_value",
    [
        ("MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL", "1 day'); DROP TABLE x"),
        ("MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL", "daily"),  # pg_partman preset, not supported
        ("MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL", "1d"),  # missing unit word
        ("MCPG_NL2SQL_AUDIT_COMPRESS_AFTER", "7 days; SELECT 1"),
        ("MCPG_NL2SQL_AUDIT_COMPRESS_AFTER", "forever"),
    ],
)
def test_nl2sql_audit_intervals_fail_fast_on_malformed_values(var: str, bad_value: str) -> None:
    """Interval strings flow into DDL as ``INTERVAL '<value>'`` and
    must match ``<digits> <unit>`` — the loader fails at startup so a
    misconfigured env never reaches the driver."""
    with pytest.raises(ConfigError, match=var):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, var: bad_value})


def test_nl2sql_audit_intervals_repr_round_trip_in_settings() -> None:
    """sourcery review: the new fields must show up in repr() so
    runtime misconfigurations are easy to spot in logs."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL": "2 hours",
            "MCPG_NL2SQL_AUDIT_COMPRESS_AFTER": "3 days",
            "MCPG_NL2SQL_AUDIT_READER_ROLE": "audit_ro",
        }
    )
    rendered = repr(settings)
    assert "nl2sql_audit_chunk_interval='2 hours'" in rendered
    assert "nl2sql_audit_compress_after='3 days'" in rendered
    assert "nl2sql_audit_reader_role='audit_ro'" in rendered


def test_nl2sql_provider_rejects_unknown_vendor() -> None:
    with pytest.raises(ConfigError, match="MCPG_NL2SQL_PROVIDER"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_PROVIDER": "cohere",
                "MCPG_NL2SQL_API_KEY": "k",
            }
        )


def test_nl2sql_discovers_openai_compatible_vendor_keys() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "DEEPSEEK_API_KEY": "d",
            "DASHSCOPE_API_KEY": "q",
            "OPENROUTER_API_KEY": "o",
            "PERPLEXITY_API_KEY": "p",
        }
    )
    keys = dict(settings.nl2sql_api_keys)
    assert keys == {"deepseek": "d", "qwen": "q", "openrouter": "o", "perplexity": "p"}
    # No original-three key present -> auto-pick falls through to the
    # OpenAI-compatible vendors in documented order.
    assert settings.nl2sql_provider == "deepseek"


def test_qwen_api_key_is_an_alias_for_dashscope() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, "QWEN_API_KEY": "via-alias"})
    assert dict(settings.nl2sql_api_keys) == {"qwen": "via-alias"}
    assert settings.nl2sql_provider == "qwen"


def test_original_vendors_keep_auto_pick_priority_over_new_ones() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "DEEPSEEK_API_KEY": "d",
            "GEMINI_API_KEY": "g",
        }
    )
    assert settings.nl2sql_provider == "gemini"


def test_custom_providers_parse_with_keys_keyless_and_key_env_override() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_CUSTOM_PROVIDERS": (
                "acme=https://api.acme.example/v1|acme-large,\n"
                "myvendor=https://api.myvendor.example/v1|big-model|MYVENDOR_TOKEN,\n"
                "ollama=http://localhost:11434/v1|llama3.1"
            ),
            "ACME_API_KEY": "gsk-key",
            "MYVENDOR_TOKEN": "mv-key",
        }
    )
    assert settings.nl2sql_custom_providers == (
        ("acme", "https://api.acme.example/v1", "acme-large"),
        ("myvendor", "https://api.myvendor.example/v1", "big-model"),
        ("ollama", "http://localhost:11434/v1", "llama3.1"),
    )
    keys = dict(settings.nl2sql_api_keys)
    assert keys["acme"] == "gsk-key"  # <NAME>_API_KEY convention
    assert keys["myvendor"] == "mv-key"  # explicit KEY_ENV_VAR segment (vendors that deviate)
    assert keys["ollama"] == "unused"  # keyless local endpoint
    # Built-ins absent -> auto-pick falls through to first declared custom.
    assert settings.nl2sql_provider == "acme"


def test_custom_provider_can_be_pinned_as_default() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_CUSTOM_PROVIDERS": "acme=https://api.acme.example/v1|acme-large",
            "MCPG_NL2SQL_PROVIDER": "acme",
            "ANTHROPIC_API_KEY": "would-otherwise-win",
        }
    )
    assert settings.nl2sql_provider == "acme"


def test_built_in_vendors_still_beat_customs_in_auto_pick() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_CUSTOM_PROVIDERS": "acme=https://api.acme.example/v1|m",
            "PERPLEXITY_API_KEY": "p",
        }
    )
    assert settings.nl2sql_provider == "perplexity"


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ("acme=https://a/v1|m, acme=https://b/v1|m", "duplicate"),
        # A name that collides with any built-in is rejected — the original
        # first-party trio and every provider in the expanded fleet.
        ("openai=https://a/v1|m", "clashes with a built-in"),
        ("groq=https://a/v1|m", "clashes with a built-in"),
        ("huggingface=https://a/v1|m", "clashes with a built-in"),
        ("acme=http://api.acme.example/v1|m", "plain HTTP is only allowed for loopback"),
        ("acme=https://api.acme.example/v1", "no default model"),
        ("acme=ftp://api.acme.example/v1|m", "must be http"),
        ("Acme Name=https://a/v1|m", "must match"),
        ("acme=https://a/v1|m|lower_case", "UPPER_SNAKE_CASE"),
    ],
)
def test_custom_provider_declarations_fail_fast_on_malformed_entries(entry: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_NL2SQL_CUSTOM_PROVIDERS": entry})


@pytest.mark.parametrize(
    ("env_var", "provider"),
    [
        ("XAI_API_KEY", "xai"),
        ("MISTRAL_API_KEY", "mistral"),
        ("HF_TOKEN", "huggingface"),  # deviates from <VENDOR>_API_KEY
        ("GITHUB_TOKEN", "github"),  # deviates
        ("DEEPINFRA_TOKEN", "deepinfra"),  # deviates
        ("SAMBANOVA_API_KEY", "sambanova"),
        ("ZAI_API_KEY", "glm"),  # deviates
        ("ARK_API_KEY", "doubao"),  # deviates
        ("SAKANA_API_KEY", "sakana"),
    ],
)
def test_expanded_fleet_provider_discovered_from_its_vendor_env_var(env_var: str, provider: str) -> None:
    # A built-in becomes configured (and, as the sole key, auto-picked) purely
    # from its vendor env var — including the ones whose var isn't <NAME>_API_KEY.
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL, env_var: "k"})
    assert dict(settings.nl2sql_api_keys).get(provider) == "k"
    assert settings.nl2sql_provider == provider


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
    assert dict(settings.nl2sql_api_keys)["anthropic"] == "explicit-key"


def test_nl2sql_falls_back_to_anthropic_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "fallback",
        }
    )
    assert dict(settings.nl2sql_api_keys)["anthropic"] == "fallback"


def test_nl2sql_falls_back_to_openai_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "openai",
            "OPENAI_API_KEY": "fallback",
        }
    )
    assert dict(settings.nl2sql_api_keys)["openai"] == "fallback"


def test_nl2sql_falls_back_to_gemini_or_google_api_key_env() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "gemini",
            "GOOGLE_API_KEY": "google-fallback",
        }
    )
    assert dict(settings.nl2sql_api_keys)["gemini"] == "google-fallback"

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-fallback",
            # GOOGLE_API_KEY also set — GEMINI_API_KEY wins because it's checked first.
            "GOOGLE_API_KEY": "google-fallback",
        }
    )
    assert dict(settings.nl2sql_api_keys)["gemini"] == "gemini-fallback"


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
    # The repr surfaces only the list of configured providers, not the keys.
    assert "nl2sql_api_keys=['anthropic']" in rendered


# --- multi-provider behaviour (the "one server, many IDEs" shape) -------


def test_nl2sql_auto_picks_anthropic_when_only_its_vendor_key_present() -> None:
    # Operator hasn't set MCPG_NL2SQL_PROVIDER; only ANTHROPIC_API_KEY in env.
    # MCPg auto-picks anthropic as the default.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "ANTHROPIC_API_KEY": "sk-ant-…",
        }
    )
    assert settings.nl2sql_provider == "anthropic"
    assert dict(settings.nl2sql_api_keys) == {"anthropic": "sk-ant-…"}


def test_nl2sql_auto_picks_in_preference_order_anthropic_openai_gemini() -> None:
    # All three keys present, no explicit provider — default to anthropic.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "ANTHROPIC_API_KEY": "sk-ant",
            "OPENAI_API_KEY": "sk-oa",
            "GEMINI_API_KEY": "sk-gm",
        }
    )
    assert settings.nl2sql_provider == "anthropic"
    assert sorted(dict(settings.nl2sql_api_keys)) == ["anthropic", "gemini", "openai"]

    # Drop anthropic — falls through to openai.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "OPENAI_API_KEY": "sk-oa",
            "GEMINI_API_KEY": "sk-gm",
        }
    )
    assert settings.nl2sql_provider == "openai"

    # Drop both — falls through to gemini.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "GEMINI_API_KEY": "sk-gm",
        }
    )
    assert settings.nl2sql_provider == "gemini"


def test_nl2sql_explicit_provider_overrides_preference_order() -> None:
    # All three configured, operator pins openai as the default.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_NL2SQL_PROVIDER": "openai",
            "ANTHROPIC_API_KEY": "sk-ant",
            "OPENAI_API_KEY": "sk-oa",
            "GEMINI_API_KEY": "sk-gm",
        }
    )
    assert settings.nl2sql_provider == "openai"
    # All three remain accessible via the tool's `provider=` arg.
    assert sorted(dict(settings.nl2sql_api_keys)) == ["anthropic", "gemini", "openai"]


def test_nl2sql_api_key_without_provider_is_rejected() -> None:
    # Without MCPG_NL2SQL_PROVIDER, MCPg can't know which provider
    # MCPG_NL2SQL_API_KEY is for — refuse to start with a clear message.
    with pytest.raises(ConfigError, match="MCPG_NL2SQL_API_KEY is set but MCPG_NL2SQL_PROVIDER"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_NL2SQL_API_KEY": "stray",
            }
        )


def test_nl2sql_no_keys_means_tool_reports_no_provider() -> None:
    # No vendor keys, no MCPG_NL2SQL_PROVIDER — Settings reports unset
    # and the tool will error at call time (not at startup).
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.nl2sql_provider is None
    assert settings.nl2sql_api_keys == ()


# --- replica routing (Phase 1.6) -----------------------------------------


def test_replica_urls_defaults_to_empty_tuple() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).replica_urls == ()


def test_replica_urls_parses_comma_separated_list() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_REPLICA_URLS": (
                "postgresql://u:p@replica-1/db?sslmode=require, postgresql://u:p@replica-2/db?sslmode=require"
            ),
        }
    )
    assert settings.replica_urls == (
        "postgresql://u:p@replica-1/db?sslmode=require",
        "postgresql://u:p@replica-2/db?sslmode=require",
    )


def test_blank_replica_urls_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_REPLICA_URLS"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_REPLICA_URLS": "  ,  "})


def test_replica_repr_obfuscates_passwords() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_REPLICA_URLS": "postgresql://u:supersecret@replica/db?sslmode=require",
        }
    )
    rendered = repr(settings)
    assert "supersecret" not in rendered


# ---------------------------------------------------------------------------
# Multi-database selector — MCPG_SECONDARY_DATABASE_URLS (roadmap 13.1)
# ---------------------------------------------------------------------------


def test_secondary_database_urls_default_empty() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).secondary_database_urls == ()


def test_secondary_database_urls_parses_name_dsn_pairs() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SECONDARY_DATABASE_URLS": (
                "analytics=postgresql://u:p@localhost/an, reporting=postgresql://u:p@localhost/rep"
            ),
        }
    )
    assert settings.secondary_database_urls == (
        ("analytics", "postgresql://u:p@localhost/an"),
        ("reporting", "postgresql://u:p@localhost/rep"),
    )


def test_secondary_database_urls_accepts_newline_separator() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SECONDARY_DATABASE_URLS": "a=postgresql://u:p@localhost/a\nb=postgresql://u:p@localhost/b",
        }
    )
    assert [name for name, _ in settings.secondary_database_urls] == ["a", "b"]


def test_secondary_database_urls_blank_raises() -> None:
    with pytest.raises(ConfigError, match="MCPG_SECONDARY_DATABASE_URLS must not be blank"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_SECONDARY_DATABASE_URLS": "  ,  "})


def test_secondary_database_urls_missing_equals_raises() -> None:
    with pytest.raises(ConfigError, match="not in name=dsn form"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_SECONDARY_DATABASE_URLS": "justaname"})


def test_secondary_database_urls_empty_dsn_raises() -> None:
    with pytest.raises(ConfigError, match="empty DSN"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_SECONDARY_DATABASE_URLS": "a="})


def test_secondary_database_urls_duplicate_name_raises() -> None:
    with pytest.raises(ConfigError, match="duplicate name 'a'"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SECONDARY_DATABASE_URLS": ("a=postgresql://u:p@localhost/x,a=postgresql://u:p@localhost/y"),
            }
        )


def test_secondary_database_urls_reserved_primary_name_raises() -> None:
    with pytest.raises(ConfigError, match="'primary' is reserved"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SECONDARY_DATABASE_URLS": "primary=postgresql://u:p@localhost/x",
            }
        )


def test_secondary_database_urls_bad_identifier_raises() -> None:
    with pytest.raises(ConfigError, match=r"must match \[a-z0-9_\]"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SECONDARY_DATABASE_URLS": "Bad-Name=postgresql://u:p@localhost/x",
            }
        )


def test_secondary_database_urls_enforces_tls_like_primary() -> None:
    with pytest.raises(ConfigError, match=r"MCPG_SECONDARY_DATABASE_URLS\[an\].*sslmode"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SECONDARY_DATABASE_URLS": "an=postgresql://u:p@remote.example/db?sslmode=disable",
            }
        )


def test_secondary_database_urls_tls_bypass_with_allow_insecure() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_ALLOW_INSECURE_TLS": "true",
            "MCPG_SECONDARY_DATABASE_URLS": "an=postgresql://u:p@remote.example/db?sslmode=disable",
        }
    )
    assert settings.secondary_database_urls == (("an", "postgresql://u:p@remote.example/db?sslmode=disable"),)


def test_secondary_database_urls_repr_obfuscates_passwords() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SECONDARY_DATABASE_URLS": "an=postgresql://u:supersecret@localhost/an",
        }
    )
    assert "supersecret" not in repr(settings)


# --- OIDC auth (Shortlist 6.5) -------------------------------------------


def test_auth_mode_defaults_to_static() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.auth_mode == "static"


def test_auth_mode_rejects_unknown_value() -> None:
    with pytest.raises(ConfigError, match="MCPG_AUTH_MODE"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_AUTH_MODE": "saml"})


def test_oidc_mode_requires_issuer_and_audience() -> None:
    with pytest.raises(ConfigError, match="MCPG_OIDC_ISSUER"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_AUTH_MODE": "oidc"})

    with pytest.raises(ConfigError, match="MCPG_OIDC_AUDIENCE"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_AUTH_MODE": "oidc",
                "MCPG_OIDC_ISSUER": "https://issuer.example",
            }
        )


def test_oidc_settings_parse_when_complete() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_AUTH_MODE": "oidc",
            "MCPG_OIDC_ISSUER": "https://issuer.example",
            "MCPG_OIDC_AUDIENCE": "mcpg",
            "MCPG_OIDC_ROLE_CLAIM": "pg_role",
        }
    )
    assert settings.auth_mode == "oidc"
    assert settings.oidc_issuer == "https://issuer.example"
    assert settings.oidc_audience == "mcpg"
    assert settings.oidc_role_claim == "pg_role"


def test_oidc_blank_individual_settings_raise() -> None:
    with pytest.raises(ConfigError, match="MCPG_OIDC_ISSUER"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_OIDC_ISSUER": "   "})


# --- PG TLS enforcement (security hardening) -----------------------------


def test_loopback_database_url_without_sslmode_is_accepted() -> None:
    # Default ``_DB_URL`` points at localhost without sslmode set;
    # the existing test suite relies on this path staying clean.
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.allow_insecure_tls is False
    assert settings.database_url == _DB_URL


def test_remote_database_url_without_sslmode_is_rejected() -> None:
    with pytest.raises(ConfigError, match="sslmode"):
        load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@db.example.com:5432/app"})


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_remote_database_url_with_insecure_sslmode_is_rejected(mode: str) -> None:
    with pytest.raises(ConfigError, match=mode):
        load_settings({"MCPG_DATABASE_URL": f"postgresql://u:p@db.example.com/app?sslmode={mode}"})


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_remote_database_url_with_enforced_sslmode_is_accepted(mode: str) -> None:
    settings = load_settings({"MCPG_DATABASE_URL": f"postgresql://u:p@db.example.com/app?sslmode={mode}"})
    assert f"sslmode={mode}" in settings.database_url


def test_remote_database_url_is_accepted_when_allow_insecure_tls_is_true() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@db.example.com/app?sslmode=disable",
            "MCPG_ALLOW_INSECURE_TLS": "true",
        }
    )
    assert settings.allow_insecure_tls is True


def test_insecure_replica_url_is_rejected() -> None:
    with pytest.raises(ConfigError, match="MCPG_REPLICA_URLS"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_REPLICA_URLS": "postgresql://u:p@replica.example.com/app?sslmode=disable",
            }
        )


def test_loopback_aliases_are_treated_as_local() -> None:
    # ``127.0.0.1`` and ``::1`` are local sockets; the validator must
    # not require sslmode for them.
    for host in ("127.0.0.1", "[::1]"):
        load_settings({"MCPG_DATABASE_URL": f"postgresql://u:p@{host}:5432/app"})


def test_allow_insecure_tls_appears_in_repr() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert "allow_insecure_tls=False" in repr(settings)


def test_keyvalue_dsn_with_insecure_sslmode_is_rejected() -> None:
    # libpq accepts keyword/value DSNs (host=... sslmode=...). The
    # previous urllib-based check failed to extract the host and
    # silently treated the DSN as loopback. Use a key-value form to
    # pin the conninfo_to_dict path.
    with pytest.raises(ConfigError, match=r"db\.example\.com"):
        load_settings({"MCPG_DATABASE_URL": "host=db.example.com sslmode=disable user=u password=p dbname=app"})


def test_multi_host_uri_with_insecure_sslmode_is_rejected() -> None:
    # ``postgresql://h1,h2/db`` is libpq's failover syntax. The
    # comma breaks urllib's host extraction; conninfo_to_dict keeps
    # the host list intact. Any non-loopback entry should fail the
    # validator.
    with pytest.raises(ConfigError, match="host1"):
        load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@host1,host2:5432,5432/app?sslmode=disable"})


def test_dsn_without_explicit_host_is_rejected() -> None:
    # An empty host means libpq falls back to ``PGHOST`` or a default
    # that may not be loopback. Refuse unless the operator explicitly
    # opts in.
    with pytest.raises(ConfigError, match="no explicit host"):
        load_settings({"MCPG_DATABASE_URL": "postgresql:///app?sslmode=disable"})


def test_dsn_without_explicit_host_is_accepted_when_allow_insecure_tls_is_true() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql:///app",
            "MCPG_ALLOW_INSECURE_TLS": "true",
        }
    )
    assert settings.allow_insecure_tls is True


def test_replica_url_error_includes_index_for_diagnostics() -> None:
    # Multiple replicas with one misconfigured entry — the error
    # message must identify WHICH replica is at fault.
    with pytest.raises(ConfigError, match=r"MCPG_REPLICA_URLS\[1\]"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_REPLICA_URLS": (
                    "postgresql://u:p@replica-1/db?sslmode=require, postgresql://u:p@replica-2/db?sslmode=disable"
                ),
            }
        )


def test_new_config_parameters_loads_and_validates() -> None:
    # Test safe defaults
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.http_max_body_bytes == 1048576
    assert settings.http_allowed_origins == ()
    assert settings.http_hsts_max_age == 31536000
    assert settings.shutdown_drain_seconds == 30
    assert settings.audit_hmac_key is None
    assert settings.audit_integrity is False

    # Test explicit parsed values
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_HTTP_MAX_BODY_BYTES": "2097152",
            "MCPG_HTTP_ALLOWED_ORIGINS": "http://localhost:3000, https://app.example.com",
            "MCPG_HTTP_HSTS_MAX_AGE": "86400",
            "MCPG_SHUTDOWN_DRAIN_SECONDS": "15",
            "MCPG_AUDIT_HMAC_KEY": "my-secret-key",
            "MCPG_AUDIT_INTEGRITY": "true",
        }
    )
    assert settings.http_max_body_bytes == 2097152
    assert settings.http_allowed_origins == ("http://localhost:3000", "https://app.example.com")
    assert settings.http_hsts_max_age == 86400
    assert settings.shutdown_drain_seconds == 15
    assert settings.audit_hmac_key == "my-secret-key"
    assert settings.audit_integrity is True

    # Test blank HMAC key raises
    with pytest.raises(ConfigError, match="MCPG_AUDIT_HMAC_KEY"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_AUDIT_HMAC_KEY": "   ",
            }
        )

    # Test audit integrity true without HMAC key raises
    with pytest.raises(ConfigError, match="MCPG_AUDIT_INTEGRITY"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_AUDIT_INTEGRITY": "true",
            }
        )

    # Test invalid HSTS max age raises
    with pytest.raises(ConfigError, match="MCPG_HTTP_HSTS_MAX_AGE"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_HTTP_HSTS_MAX_AGE": "-10",
            }
        )

    # Test obfuscated audit HMAC key in repr
    assert "my-secret-key" not in repr(settings)
    assert "audit_hmac_key='set'" in repr(settings)


# --- subprocess hardening + HTTP request timeout -------------------------


def test_subprocess_hardening_defaults_to_open() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.subprocess_bin_allowlist == ()
    assert settings.subprocess_cpu_seconds is None
    assert settings.subprocess_memory_mb is None


def test_subprocess_hardening_parses_allowlist_and_limits() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SUBPROCESS_BIN_ALLOWLIST": "/usr/bin, /usr/local/bin",
            "MCPG_SUBPROCESS_CPU_SECONDS": "30",
            "MCPG_SUBPROCESS_MEMORY_MB": "512",
        }
    )
    assert settings.subprocess_bin_allowlist == ("/usr/bin", "/usr/local/bin")
    assert settings.subprocess_cpu_seconds == 30
    assert settings.subprocess_memory_mb == 512


def test_subprocess_bin_allowlist_rejects_relative_paths() -> None:
    with pytest.raises(ConfigError, match="MCPG_SUBPROCESS_BIN_ALLOWLIST"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SUBPROCESS_BIN_ALLOWLIST": "/usr/bin, relative/dir",
            }
        )


def test_subprocess_cpu_seconds_rejects_non_positive() -> None:
    with pytest.raises(ConfigError, match="MCPG_SUBPROCESS_CPU_SECONDS"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_SUBPROCESS_CPU_SECONDS": "0",
            }
        )


def test_http_request_timeout_defaults_to_zero_and_parses() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).http_request_timeout_seconds == 0

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_HTTP_REQUEST_TIMEOUT_SECONDS": "20",
        }
    )
    assert settings.http_request_timeout_seconds == 20


def test_http_request_timeout_rejects_negative() -> None:
    with pytest.raises(ConfigError, match="MCPG_HTTP_REQUEST_TIMEOUT_SECONDS"):
        load_settings(
            {
                "MCPG_DATABASE_URL": _DB_URL,
                "MCPG_HTTP_REQUEST_TIMEOUT_SECONDS": "-5",
            }
        )
