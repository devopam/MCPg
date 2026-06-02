"""Tests for the pluggable secrets backend (mcpg.secrets) + its config wiring."""

import json

import pytest

from mcpg.config import ConfigError, load_settings
from mcpg.secrets import (
    EnvSecretsProvider,
    FileSecretsProvider,
    SecretsError,
    build_secrets_provider,
)

_DB_URL = "postgresql://user:secret@localhost:5432/app"


# --- provider selection ----------------------------------------------------


def test_default_backend_is_env() -> None:
    provider, _backend = build_secrets_provider({"FOO": "bar"})
    assert isinstance(provider, EnvSecretsProvider)
    assert provider.get("FOO") == "bar"
    assert provider.get("MISSING") is None


def test_unknown_backend_raises() -> None:
    with pytest.raises(SecretsError, match="MCPG_SECRETS_BACKEND"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "vault"})


def test_file_backend_requires_a_path() -> None:
    with pytest.raises(SecretsError, match="MCPG_SECRETS_FILE_PATH"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file"})


# --- file backend ----------------------------------------------------------


def test_file_backend_loads_json_and_overlays_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"ANTHROPIC_API_KEY": "from-file", "MCPG_AUDIT_HMAC_KEY": "hmac-from-file"}))

    env = {
        "MCPG_SECRETS_BACKEND": "file",
        "MCPG_SECRETS_FILE_PATH": str(path),
        # Present in env but overridden by the file:
        "ANTHROPIC_API_KEY": "from-env",
        # Not in the file — must fall through to env:
        "OPENAI_API_KEY": "openai-from-env",
    }
    provider, _backend = build_secrets_provider(env)
    assert isinstance(provider, FileSecretsProvider)
    assert provider.get("ANTHROPIC_API_KEY") == "from-file"  # file wins
    assert provider.get("MCPG_AUDIT_HMAC_KEY") == "hmac-from-file"
    assert provider.get("OPENAI_API_KEY") == "openai-from-env"  # env fallback
    assert provider.get("NOPE") is None


def test_file_backend_coerces_scalar_values_to_str(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"PORT_SECRET": 5432, "FLAG": True}))
    provider, _backend = build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    assert provider.get("PORT_SECRET") == "5432"
    assert provider.get("FLAG") == "True"


def test_file_backend_rejects_non_object_top_level(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(["not", "a", "map"]))
    with pytest.raises(SecretsError, match="name -> value"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})


def test_file_backend_rejects_nested_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"KEY": {"nested": "object"}}))
    with pytest.raises(SecretsError, match="must be a scalar"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})


def test_file_backend_rejects_malformed_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text("{not valid json")
    with pytest.raises(SecretsError, match="not valid JSON") as excinfo:
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    # The parse-error message must NOT echo source text — only the file
    # path + line/column. (Sourcery security note on PR #41.)
    assert "not valid json" not in str(excinfo.value)


def test_file_backend_parse_error_does_not_leak_source_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A malformed JSON file whose error point lies inside what could be a
    # secret value: a stock JSONDecodeError message would include the
    # offending character. Our wrapper must keep it out of the message.
    path = tmp_path / "secrets.json"
    path.write_text('{"PASSWORD": "hunter2-with-stray-quote " extra"}')
    with pytest.raises(SecretsError, match="not valid JSON") as excinfo:
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "PASSWORD" not in message


def test_file_backend_yaml_parse_error_does_not_leak_source_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("yaml")
    path = tmp_path / "secrets.yaml"
    # Unbalanced quote inside what could be a secret value.
    path.write_text('PASSWORD: "hunter2-unclosed\nOTHER: ok\n')
    with pytest.raises(SecretsError, match="not valid YAML") as excinfo:
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "PASSWORD" not in message


def test_file_backend_missing_file_raises() -> None:
    with pytest.raises(SecretsError, match="could not read"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": "/nonexistent/secrets.json"})


def test_file_backend_loads_yaml_when_pyyaml_available(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("yaml")
    path = tmp_path / "secrets.yaml"
    path.write_text("ANTHROPIC_API_KEY: sk-ant-yaml\nMCPG_HTTP_AUTH_TOKEN: tok-yaml\n")
    provider, _backend = build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    assert provider.get("ANTHROPIC_API_KEY") == "sk-ant-yaml"
    assert provider.get("MCPG_HTTP_AUTH_TOKEN") == "tok-yaml"


def test_file_backend_rejects_malformed_yaml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("yaml")
    path = tmp_path / "secrets.yaml"
    path.write_text("key: : : broken\n  bad indent")
    with pytest.raises(SecretsError, match="not valid YAML"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})


def test_file_backend_rejects_non_string_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("yaml")
    # YAML permits non-string mapping keys (JSON does not).
    path = tmp_path / "secrets.yaml"
    path.write_text("1: oops\n")
    with pytest.raises(SecretsError, match="non-string key"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})


def test_file_backend_skips_null_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"PRESENT": "yes", "ABSENT": None}))
    provider, _backend = build_secrets_provider({"MCPG_SECRETS_BACKEND": "file", "MCPG_SECRETS_FILE_PATH": str(path)})
    assert provider.get("PRESENT") == "yes"
    # A null value is skipped entirely, so lookup falls through to env (None here).
    assert provider.get("ABSENT") is None


# --- config integration ----------------------------------------------------


def test_load_settings_defaults_to_env_backend() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _DB_URL})
    assert settings.secrets_backend == "env"


def test_load_settings_resolves_secrets_from_a_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps(
            {
                "MCPG_HTTP_AUTH_TOKEN": "tok-from-file",
                "ANTHROPIC_API_KEY": "sk-ant-from-file",
                "MCPG_AUDIT_HMAC_KEY": "hmac-from-file",
            }
        )
    )
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SECRETS_BACKEND": "file",
            "MCPG_SECRETS_FILE_PATH": str(path),
            "MCPG_AUDIT_INTEGRITY": "true",
        }
    )

    assert settings.secrets_backend == "file"
    assert settings.http_auth_token == "tok-from-file"
    assert dict(settings.nl2sql_api_keys)["anthropic"] == "sk-ant-from-file"
    assert settings.audit_hmac_key == "hmac-from-file"
    # The default provider is auto-picked from the file-supplied key.
    assert settings.nl2sql_provider == "anthropic"


def test_load_settings_surfaces_secrets_errors_as_config_errors() -> None:
    with pytest.raises(ConfigError, match="MCPG_SECRETS_BACKEND"):
        load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_SECRETS_BACKEND": "bogus"})


def test_secrets_backend_does_not_leak_secret_values_in_repr(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"MCPG_HTTP_AUTH_TOKEN": "super-secret-token"}))
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_SECRETS_BACKEND": "file",
            "MCPG_SECRETS_FILE_PATH": str(path),
        }
    )
    rendered = repr(settings)
    assert "super-secret-token" not in rendered
    assert "secrets_backend='file'" in rendered
