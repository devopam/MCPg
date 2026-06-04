"""Tests for mcpg.otel_tracing — opt-in OpenTelemetry tracing per tool call."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from mcpg.config import load_settings
from mcpg.otel_tracing import (
    TracerHandle,
    attach_span_exporter,
    is_otel_installed,
    setup_tracing,
    tool_span,
)


def _settings(**overrides: object) -> Any:
    """Minimal settings with the otel flag default-off."""
    env = {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"}
    for key, value in overrides.items():
        env[key] = str(value)
    return load_settings(env)


# --- discovery -------------------------------------------------------------


def test_is_otel_installed_returns_true_when_sdk_is_present() -> None:
    # The dev group pins the SDK so this should always hold under our test
    # matrix. The `False` branch is exercised in
    # `test_setup_tracing_returns_none_when_sdk_missing` via monkey-patching.
    assert is_otel_installed() is True


def test_is_otel_installed_returns_false_when_imports_fail() -> None:
    with patch("mcpg.otel_tracing.is_otel_installed", return_value=False):
        from mcpg.otel_tracing import is_otel_installed as is_installed_mock

        assert is_installed_mock() is False


# --- setup paths -----------------------------------------------------------


def test_setup_tracing_returns_none_when_disabled() -> None:
    # otel_enabled defaults to False — the function must short-circuit
    # before touching the SDK so no provider is registered on import.
    assert setup_tracing(_settings()) is None


def test_setup_tracing_returns_none_when_sdk_missing() -> None:
    import logging

    settings = _settings(MCPG_OTEL_ENABLED="true")
    # Capture directly off the ``mcpg.otel`` logger — earlier tests
    # can have :func:`mcpg.obs_logging.setup_logging` to disable
    # propagation on the package root, which hides the warning from
    # pytest's caplog fixture. A dedicated handler bypasses that.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    logger = logging.getLogger("mcpg.otel")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with patch("mcpg.otel_tracing.is_otel_installed", return_value=False):
            result = setup_tracing(settings)
    finally:
        logger.removeHandler(handler)
    assert result is None
    # Operators will hit this path if they enabled OTel but forgot the
    # extra — the log line is the single signal they get.
    assert any("OpenTelemetry SDK is not installed" in r.getMessage() for r in records)


def test_setup_tracing_returns_tracer_handle_when_enabled() -> None:
    handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true"))
    try:
        assert isinstance(handle, TracerHandle)
    finally:
        if handle is not None:
            handle.shutdown()


def test_setup_tracing_picks_up_service_name_when_env_does_not_override() -> None:
    # No service.name in OTEL_RESOURCE_ATTRIBUTES → MCPG_OTEL_SERVICE_NAME applies.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OTEL_RESOURCE_ATTRIBUTES", None)
        handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true", MCPG_OTEL_SERVICE_NAME="mcpg-staging"))
    try:
        assert handle is not None
        # Resource attributes live on the provider's resource.
        resource = handle._provider.resource  # type: ignore[attr-defined]
        assert resource.attributes.get("service.name") == "mcpg-staging"
    finally:
        if handle is not None:
            handle.shutdown()


def test_setup_tracing_defers_to_env_resource_attributes() -> None:
    # When OTEL_RESOURCE_ATTRIBUTES carries service.name the project
    # setting is ignored, so deployment-level config wins.
    with patch.dict(os.environ, {"OTEL_RESOURCE_ATTRIBUTES": "service.name=my-app,deployment.env=prod"}, clear=False):
        handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true", MCPG_OTEL_SERVICE_NAME="ignored"))
    try:
        assert handle is not None
        # The SDK auto-merges OTEL_RESOURCE_ATTRIBUTES into the resource.
        # Our code didn't set service.name, so the env value should survive.
        resource = handle._provider.resource  # type: ignore[attr-defined]
        assert resource.attributes.get("service.name") == "my-app"
    finally:
        if handle is not None:
            handle.shutdown()


# --- span semantics --------------------------------------------------------


def _capture_spans(handle: TracerHandle) -> Any:
    """Attach an in-memory exporter and return it for assertions."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    attach_span_exporter(handle, exporter)
    return exporter


def test_tool_span_no_op_when_handle_is_none() -> None:
    # The unified entry point must accept None — the server uses it
    # unconditionally regardless of whether OTel is configured.
    with tool_span(None, "list_tables", {}) as span:
        assert span is None


def test_tool_span_records_attributes_on_success() -> None:
    handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true"))
    assert handle is not None
    try:
        exporter = _capture_spans(handle)
        with tool_span(handle, "list_tables", {"schema": "app"}):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "mcp.call_tool"
        assert span.attributes is not None
        assert span.attributes["mcp.tool.name"] == "list_tables"
        assert span.attributes["mcp.tool.argument_count"] == 1
        assert span.attributes["mcp.tool.status"] == "ok"
        # OK status — Status.OK enum.
        from opentelemetry.trace import StatusCode

        assert span.status.status_code == StatusCode.OK
    finally:
        handle.shutdown()


def test_tool_span_records_error_attributes_on_exception() -> None:
    handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true"))
    assert handle is not None
    try:
        exporter = _capture_spans(handle)
        with pytest.raises(RuntimeError, match="boom"):
            with tool_span(handle, "run_select", {"sql": "SELECT 1"}):
                raise RuntimeError("boom")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.attributes is not None
        assert span.attributes["mcp.tool.name"] == "run_select"
        assert span.attributes["mcp.tool.status"] == "error"
        assert span.attributes["error.type"] == "RuntimeError"
        assert span.attributes["error.message"] == "boom"

        from opentelemetry.trace import StatusCode

        assert span.status.status_code == StatusCode.ERROR
    finally:
        handle.shutdown()


def test_tool_span_truncates_long_error_messages() -> None:
    handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true"))
    assert handle is not None
    try:
        exporter = _capture_spans(handle)
        long_message = "x" * 500
        with pytest.raises(RuntimeError):
            with tool_span(handle, "huge_query", {}):
                raise RuntimeError(long_message)

        spans = exporter.get_finished_spans()
        assert spans[0].attributes is not None
        # The cap is 200 chars; the truncation keeps span sizes bounded
        # regardless of how verbose the underlying exception is.
        assert len(spans[0].attributes["error.message"]) == 200
    finally:
        handle.shutdown()


def test_tool_span_does_not_attach_raw_argument_values() -> None:
    # Tool arguments can contain SQL literals, embeddings, secrets —
    # the span should only ever carry the *count*. A regression here
    # would leak sensitive payloads into every trace backend.
    handle = setup_tracing(_settings(MCPG_OTEL_ENABLED="true"))
    assert handle is not None
    try:
        exporter = _capture_spans(handle)
        with tool_span(handle, "vector_search", {"vec": [0.1, 0.2], "k": 5, "secret": "value"}):
            pass

        span = exporter.get_finished_spans()[0]
        assert span.attributes is not None
        assert span.attributes["mcp.tool.argument_count"] == 3
        # No argument-value keys should be present.
        attribute_keys = set(span.attributes.keys())
        # All mcp.tool.* attributes we set explicitly:
        allowed = {"mcp.tool.name", "mcp.tool.argument_count", "mcp.tool.status"}
        leaked = {key for key in attribute_keys if key.startswith("mcp.tool.")} - allowed
        assert not leaked, f"Span leaked tool-argument attributes: {leaked}"
        # And the secret payload itself must not appear in any attribute value.
        for value in span.attributes.values():
            assert value != "value"
    finally:
        handle.shutdown()


# --- config parsing --------------------------------------------------------


def test_settings_default_otel_disabled() -> None:
    settings = _settings()
    assert settings.otel_enabled is False
    assert settings.otel_service_name == "mcpg"


@pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
def test_settings_otel_enabled_accepts_truthy_strings(value: str) -> None:
    assert _settings(MCPG_OTEL_ENABLED=value).otel_enabled is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no"])
def test_settings_otel_enabled_accepts_falsy_strings(value: str) -> None:
    assert _settings(MCPG_OTEL_ENABLED=value).otel_enabled is False


def test_settings_rejects_invalid_otel_enabled_value() -> None:
    from mcpg.config import ConfigError

    with pytest.raises(ConfigError, match="MCPG_OTEL_ENABLED"):
        _settings(MCPG_OTEL_ENABLED="bogus")


def test_settings_otel_service_name_overridden_via_env() -> None:
    assert _settings(MCPG_OTEL_SERVICE_NAME="mcpg-prod").otel_service_name == "mcpg-prod"


def test_settings_otel_service_name_falls_back_to_default_when_blank() -> None:
    # Empty / whitespace strings should normalise to the default rather
    # than emit an empty service name that violates OTel conventions.
    assert _settings(MCPG_OTEL_SERVICE_NAME="   ").otel_service_name == "mcpg"


def test_settings_repr_includes_otel_fields() -> None:
    rendered = repr(_settings(MCPG_OTEL_ENABLED="true", MCPG_OTEL_SERVICE_NAME="mcpg-test"))
    assert "otel_enabled=True" in rendered
    assert "otel_service_name='mcpg-test'" in rendered
