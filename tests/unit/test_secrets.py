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
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "azure"})


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


# --- cloud backends --------------------------------------------------------


def test_vault_backend_requires_addr_and_token() -> None:
    with pytest.raises(SecretsError, match="MCPG_VAULT_ADDR"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "vault"})
    with pytest.raises(SecretsError, match="MCPG_VAULT_TOKEN"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "vault", "MCPG_VAULT_ADDR": "http://vault:8200"})


def test_vault_provider_fetches_field_and_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpg.secrets import VaultSecretsProvider

    # A fake hvac module wired into ``sys.modules`` so the provider's
    # lazy import picks it up without the real hvac client opening a
    # socket. The provider passes path / token / namespace; we record
    # them and respond with a fixed-shape KV v2 payload.
    fake_calls: list[dict[str, object]] = []

    class _FakeKVv2:
        def __init__(self) -> None:
            self.store = {
                "secret/mcpg/MCPG_DATABASE_URL": {"value": "postgresql://from-vault/x"},
                "secret/mcpg/auth": {"token": "vault-token"},
            }

        def read_secret_version(self, path: str) -> dict[str, object]:
            fake_calls.append({"path": path})
            data = self.store.get(path)
            if data is None:
                raise RuntimeError("not found")
            return {"data": {"data": data}}

    class _FakeKV:
        def __init__(self) -> None:
            self.v2 = _FakeKVv2()

    class _FakeSecrets:
        def __init__(self) -> None:
            self.kv = _FakeKV()

    class _FakeClient:
        def __init__(self, *, url: str, token: str, namespace: str | None) -> None:
            fake_calls.append({"init": (url, token, namespace)})
            self.secrets = _FakeSecrets()

        def is_authenticated(self) -> bool:
            return True

    import sys
    import types

    fake_hvac = types.ModuleType("hvac")
    fake_hvac.Client = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hvac", fake_hvac)

    provider = VaultSecretsProvider(
        addr="http://vault:8200",
        token="root",
        env={"FALLBACK_ONLY": "from-env"},
        namespace="team-a",
    )

    # Vault hit — value returned.
    assert provider.get("MCPG_DATABASE_URL") == "postgresql://from-vault/x"
    # Sub-path with explicit field name.
    assert provider.get("auth/token") == "vault-token"
    # Not in Vault → env fallback.
    assert provider.get("FALLBACK_ONLY") == "from-env"
    # Neither in Vault nor env → None.
    assert provider.get("WHO_KNOWS") is None
    # Namespace + URL were threaded through.
    init_call = next(c for c in fake_calls if "init" in c)
    assert init_call["init"] == ("http://vault:8200", "root", "team-a")
    # Cached: a repeat lookup doesn't trigger another read_secret_version.
    before = sum("path" in c for c in fake_calls)
    assert provider.get("MCPG_DATABASE_URL") == "postgresql://from-vault/x"
    after = sum("path" in c for c in fake_calls)
    assert before == after


def test_vault_provider_surfaces_install_hint_when_hvac_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend hvac isn't installed by purging it from sys.modules and
    # blocking the import. ``ImportError`` percolates up to the
    # provider's SecretsError-with-installation-hint path.
    import sys

    from mcpg.secrets import VaultSecretsProvider

    monkeypatch.setitem(sys.modules, "hvac", None)

    provider = VaultSecretsProvider(addr="http://vault:8200", token="x", env={})
    with pytest.raises(SecretsError, match="mcpg\\[vault\\]"):
        provider.get("ANY")


def test_aws_backend_constructs_with_optional_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from mcpg.secrets import AWSSecretsProvider

    fake_calls: list[dict[str, object]] = []

    class _FakeClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803  (boto3 API name)
            fake_calls.append({"SecretId": SecretId})
            # Provider can read both JSON-bundled and single-string secrets.
            payloads = {
                "mcpg/MCPG_DATABASE_URL": json.dumps({"MCPG_DATABASE_URL": "postgresql://aws/x"}),
                "mcpg/SINGLE": "plain-value",
            }
            value = payloads.get(SecretId)
            if value is None:
                raise RuntimeError("not found")
            return {"SecretString": value}

    fake_boto3_module = types.ModuleType("boto3")
    fake_boto3_module.client = lambda *_a, **_kw: _FakeClient()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3_module)

    provider = AWSSecretsProvider(
        region="us-east-1",
        env={"FALLBACK_ONLY": "from-env"},
        prefix="mcpg/",
    )
    # JSON-bundled lookup matches the requested name field.
    assert provider.get("MCPG_DATABASE_URL") == "postgresql://aws/x"
    # Single-value secrets come back verbatim.
    assert provider.get("SINGLE") == "plain-value"
    # Not in AWS → env fallback.
    assert provider.get("FALLBACK_ONLY") == "from-env"
    # The prefix was applied to every fetch.
    assert all(str(c["SecretId"]).startswith("mcpg/") for c in fake_calls)


def test_aws_provider_surfaces_install_hint_when_boto3_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from mcpg.secrets import AWSSecretsProvider

    monkeypatch.setitem(sys.modules, "boto3", None)

    provider = AWSSecretsProvider(region="us-east-1", env={})
    with pytest.raises(SecretsError, match="mcpg\\[aws\\]"):
        provider.get("ANY")


def test_gcp_backend_requires_project_id() -> None:
    with pytest.raises(SecretsError, match="MCPG_GCP_PROJECT_ID"):
        build_secrets_provider({"MCPG_SECRETS_BACKEND": "gcp"})


def test_gcp_provider_decodes_payload_and_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from mcpg.secrets import GCPSecretsProvider

    fake_calls: list[dict[str, object]] = []

    class _Payload:
        def __init__(self, data: bytes) -> None:
            self.data = data

    class _Response:
        def __init__(self, payload: _Payload | None) -> None:
            self.payload = payload

    class _FakeClient:
        def access_secret_version(self, *, name: str) -> _Response:
            fake_calls.append({"name": name})
            if name.endswith("/MCPG_DATABASE_URL/versions/latest"):
                return _Response(_Payload(b"postgresql://from-gcp/x"))
            raise RuntimeError("not found")

    class _FakeSecretManagerModule:
        SecretManagerServiceClient = _FakeClient

    fake_google = types.ModuleType("google")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.secretmanager = _FakeSecretManagerModule()  # type: ignore[attr-defined]
    fake_google.cloud = fake_google_cloud  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)

    provider = GCPSecretsProvider(
        project_id="my-project",
        env={"FALLBACK_ONLY": "from-env"},
    )
    assert provider.get("MCPG_DATABASE_URL") == "postgresql://from-gcp/x"
    assert provider.get("FALLBACK_ONLY") == "from-env"
    assert any("projects/my-project" in str(c["name"]) for c in fake_calls)


def test_gcp_provider_surfaces_install_hint_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from mcpg.secrets import GCPSecretsProvider

    monkeypatch.setitem(sys.modules, "google.cloud", None)

    provider = GCPSecretsProvider(project_id="p", env={})
    with pytest.raises(SecretsError, match="mcpg\\[gcp\\]"):
        provider.get("ANY")


# --- cloud-backend error semantics -----------------------------------------


def test_vault_provider_raises_secrets_error_on_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    # Auth / permission failures must not silently fall through to
    # env — they're operator-visible problems, not "secret missing".
    import sys
    import types

    from mcpg.secrets import VaultSecretsProvider

    class _Forbidden(Exception):  # noqa: N818  (mirrors hvac.exceptions.Forbidden naming)
        pass

    fake_hvac_exceptions = types.ModuleType("hvac.exceptions")
    fake_hvac_exceptions.Forbidden = _Forbidden  # type: ignore[attr-defined]
    fake_hvac_exceptions.Unauthorized = type("_Unauthorized", (Exception,), {})  # type: ignore[attr-defined]

    fake_hvac = types.ModuleType("hvac")
    fake_hvac.exceptions = fake_hvac_exceptions  # type: ignore[attr-defined]

    class _RaisingClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            class _Sec:
                class _Kv:
                    class _V2:
                        @staticmethod
                        def read_secret_version(path: str) -> None:
                            raise _Forbidden("permission denied")

                    v2 = _V2()

                kv = _Kv()

            self.secrets = _Sec()

    fake_hvac.Client = _RaisingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hvac", fake_hvac)
    monkeypatch.setitem(sys.modules, "hvac.exceptions", fake_hvac_exceptions)

    provider = VaultSecretsProvider(addr="http://vault:8200", token="bad", env={"FALLBACK": "from-env"})
    with pytest.raises(SecretsError, match="Vault denied access"):
        provider.get("SOMETHING")


def test_aws_provider_raises_secrets_error_on_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from mcpg.secrets import AWSSecretsProvider

    class _ClientError(Exception):
        def __init__(self, response: dict[str, object]) -> None:
            self.response = response
            super().__init__(str(response))

    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
    fake_botocore_exceptions.ClientError = _ClientError  # type: ignore[attr-defined]
    fake_botocore = types.ModuleType("botocore")
    fake_botocore.exceptions = fake_botocore_exceptions  # type: ignore[attr-defined]

    class _DenyingClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803  (boto3 API name)
            raise _ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}})

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *_a, **_kw: _DenyingClient()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_botocore_exceptions)

    provider = AWSSecretsProvider(region="us-east-1", env={"FALLBACK": "from-env"})
    with pytest.raises(SecretsError, match="AccessDeniedException"):
        provider.get("ANY")


def test_aws_provider_falls_back_to_raw_json_when_key_not_in_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Service-account JSON blobs / OAuth payloads don't have a top-
    # level key matching the secret name; the provider must surface
    # the raw JSON string rather than returning None.
    import sys
    import types

    from mcpg.secrets import AWSSecretsProvider

    class _FakeClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
            return {"SecretString": json.dumps({"client_id": "x", "private_key": "y"})}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *_a, **_kw: _FakeClient()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    provider = AWSSecretsProvider(region="us-east-1", env={})
    result = provider.get("GCP_SERVICE_ACCOUNT_JSON")
    assert result is not None
    assert '"client_id": "x"' in result
    assert '"private_key": "y"' in result


def test_gcp_provider_raises_secrets_error_on_permission_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from mcpg.secrets import GCPSecretsProvider

    class _PermissionDenied(Exception):  # noqa: N818  (mirrors google.api_core.exceptions naming)
        pass

    fake_api_core_exceptions = types.ModuleType("google.api_core.exceptions")
    fake_api_core_exceptions.PermissionDenied = _PermissionDenied  # type: ignore[attr-defined]
    fake_api_core_exceptions.Unauthenticated = type("_Unauthenticated", (Exception,), {})  # type: ignore[attr-defined]
    fake_api_core = types.ModuleType("google.api_core")
    fake_api_core.exceptions = fake_api_core_exceptions  # type: ignore[attr-defined]

    class _DenyingClient:
        def access_secret_version(self, *, name: str) -> object:
            raise _PermissionDenied("perm denied")

    class _FakeSecretManager:
        SecretManagerServiceClient = _DenyingClient

    fake_google = types.ModuleType("google")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.secretmanager = _FakeSecretManager()  # type: ignore[attr-defined]
    fake_google.cloud = fake_google_cloud  # type: ignore[attr-defined]
    fake_google.api_core = fake_api_core  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.api_core", fake_api_core)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", fake_api_core_exceptions)

    provider = GCPSecretsProvider(project_id="p", env={"FALLBACK": "from-env"})
    with pytest.raises(SecretsError, match="GCP Secret Manager denied"):
        provider.get("ANY")


# --- bounded-LRU cache (scalability P1) ------------------------------------


def test_cloud_secret_caches_evict_oldest_when_capacity_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for deep-review scalability P1 #6: the three cloud
    providers held an unbounded ``dict[str, str | None]`` cache. A
    caller hitting ``provider.get(unique_name)`` in a loop would grow
    it until OOM. The fix is a bounded LRU keyed by name.

    Asserted directly against the OrderedDict + the shared
    ``_SECRET_CACHE_MAX_ENTRIES`` constant: cap the constant down
    via monkeypatch, fill past the cap, and prove the oldest entry
    fell out while a recent one survived."""
    from mcpg import secrets as secrets_mod
    from mcpg.secrets import VaultSecretsProvider, _cache_put

    # Bound to 3 so the test is deterministic without a 1024-entry loop.
    monkeypatch.setattr(secrets_mod, "_SECRET_CACHE_MAX_ENTRIES", 3)

    provider = VaultSecretsProvider(addr="http://vault:8200", token="x", env={})
    for n, v in [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d")]:
        _cache_put(provider._cache, n, v)

    assert list(provider._cache) == ["B", "C", "D"]
    assert "A" not in provider._cache


def test_cloud_secret_cache_moves_entry_to_end_on_get_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LRU semantics: a name that was recently *read* survives an
    eviction round that drops a colder name. Tested via the shared
    helpers so the assertion holds for every provider."""
    from mcpg import secrets as secrets_mod
    from mcpg.secrets import _cache_get, _cache_put

    monkeypatch.setattr(secrets_mod, "_SECRET_CACHE_MAX_ENTRIES", 3)

    from collections import OrderedDict

    cache: OrderedDict[str, str | None] = OrderedDict()
    for n, v in [("A", "a"), ("B", "b"), ("C", "c")]:
        _cache_put(cache, n, v)
    # Touch A so it becomes most-recent. B is now the oldest.
    val, present = _cache_get(cache, "A")
    assert present and val == "a"
    # Insert D → B falls out (was oldest), A survives.
    _cache_put(cache, "D", "d")
    assert list(cache) == ["C", "A", "D"]


def test_cloud_secret_cache_preserves_none_entries() -> None:
    """A name explicitly cached as ``None`` (meaning "not in the
    backend") must survive eviction the same as a real value, and
    ``_cache_get`` must distinguish "cached as None" from "not
    cached" via the ``present`` flag — otherwise the env-fallback
    path would re-fetch on every call."""
    from collections import OrderedDict

    from mcpg.secrets import _cache_get, _cache_put

    cache: OrderedDict[str, str | None] = OrderedDict()
    _cache_put(cache, "ABSENT_IN_VAULT", None)
    val, present = _cache_get(cache, "ABSENT_IN_VAULT")
    assert present is True
    assert val is None
    val, present = _cache_get(cache, "NEVER_LOOKED_UP")
    assert present is False
    assert val is None
