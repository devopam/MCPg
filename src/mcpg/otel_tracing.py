"""OpenTelemetry tracing — one span per MCP tool call.

This module is **optional**. Production deployments install the SDK via
the ``mcpg[otel]`` extra and flip ``MCPG_OTEL_ENABLED=true``; when
either is missing the helpers here become no-ops with negligible
overhead, so the call site doesn't need to branch.

Spans live under the ``mcpg.tools`` tracer and carry:

- ``mcp.tool.name`` — the tool identifier passed to ``call_tool``.
- ``mcp.tool.argument_count`` — number of arguments supplied. We
  deliberately don't attach the raw values: tool arguments can carry
  secrets / PII (e.g. SQL with literals, embeddings, connection
  strings), so dumping them into a trace backend would be a privacy
  regression.
- ``mcp.tool.status`` — ``ok`` on success, ``error`` on exception.
- ``error.type`` / ``error.message`` on failure (message truncated at
  200 chars to keep span size bounded).

The Span status is set to OK / ERROR alongside the attributes so
backends that surface that field (Honeycomb, Tempo, Datadog, …) light
up failure cases without parsing attribute text.

Configuration is intentionally minimal: ``MCPG_OTEL_ENABLED`` and
``MCPG_OTEL_SERVICE_NAME`` are the only project-specific knobs. Every
other setting (collector endpoint, headers, resource attributes,
sampler) comes from the standard ``OTEL_*`` env vars so existing
operational tooling Just Works.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcpg.config import Settings

_TRACER_NAME = "mcpg.tools"
_SPAN_NAME = "mcp.call_tool"
_ERROR_MESSAGE_CAP = 200

_logger = logging.getLogger("mcpg.otel")


def is_otel_installed() -> bool:
    """Return ``True`` when the OpenTelemetry SDK is importable."""
    import importlib.util

    return all(
        importlib.util.find_spec(name) is not None for name in ("opentelemetry.trace", "opentelemetry.sdk.trace")
    )


class TracerHandle:
    """Wrapper around an OpenTelemetry tracer + its SDK provider.

    The handle exists for two reasons:

    1. It hides the OTel imports behind a small API so call sites
       don't need to deal with the SDK shape (and can still be tested
       when the SDK isn't installed).
    2. It owns the provider so shutdown can flush any pending spans
       before the server exits.
    """

    def __init__(self, tracer: Any, provider: Any) -> None:
        self._tracer = tracer
        self._provider = provider

    @contextlib.contextmanager
    def tool_span(self, tool_name: str, arguments: dict[str, Any]) -> Iterator[Any]:
        """Start a span for one tool invocation."""
        # Importing inside the method keeps the module importable when
        # the SDK isn't installed; the `is_otel_installed` guard on the
        # setup path means we only reach here when it is.
        from opentelemetry.trace import Status, StatusCode

        with self._tracer.start_as_current_span(_SPAN_NAME) as span:
            span.set_attribute("mcp.tool.name", tool_name)
            span.set_attribute("mcp.tool.argument_count", len(arguments))
            try:
                yield span
            except Exception as exc:
                message = str(exc)[:_ERROR_MESSAGE_CAP]
                span.set_attribute("mcp.tool.status", "error")
                span.set_attribute("error.type", type(exc).__name__)
                span.set_attribute("error.message", message)
                span.set_status(Status(StatusCode.ERROR, message))
                raise
            span.set_attribute("mcp.tool.status", "ok")
            span.set_status(Status(StatusCode.OK))

    def shutdown(self) -> None:
        """Flush + shut down the underlying SDK provider."""
        self._provider.shutdown()


def setup_tracing(settings: Settings) -> TracerHandle | None:
    """Build a :class:`TracerHandle` from ``settings``, or return ``None``.

    Returns ``None`` (and logs at WARNING for the gotcha case) when
    OTel is disabled or the SDK isn't installed — callers can use
    :func:`tool_span` unconditionally either way.

    The standard ``OTEL_*`` env vars (``OTEL_EXPORTER_OTLP_ENDPOINT``,
    ``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_RESOURCE_ATTRIBUTES``,
    ``OTEL_TRACES_SAMPLER`` …) take precedence over the project's
    knobs. We only set the ``service.name`` resource attribute from
    ``settings.otel_service_name`` when the user hasn't already
    overridden it via ``OTEL_RESOURCE_ATTRIBUTES``.
    """
    if not settings.otel_enabled:
        return None
    if not is_otel_installed():
        _logger.warning(
            "MCPG_OTEL_ENABLED=true but the OpenTelemetry SDK is not installed. "
            "Install with `pip install 'mcpg[otel]'` or set MCPG_OTEL_ENABLED=false."
        )
        return None

    from opentelemetry import trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Build the resource. OTEL_RESOURCE_ATTRIBUTES wins per spec; we
    # only set service.name when the env doesn't already carry one.
    env_attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    attrs: dict[str, Any] = {}
    if "service.name" not in env_attrs:
        attrs[SERVICE_NAME] = settings.otel_service_name
    resource = Resource.create(attrs)

    provider = TracerProvider(resource=resource)

    # Only attach the OTLP HTTP exporter when one is genuinely
    # available. In test scope we leave the provider exporter-less
    # so tests can inject an InMemorySpanExporter via
    # `attach_span_exporter` without fighting a BatchSpanProcessor.
    if _otlp_endpoint_configured():
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:
            _logger.warning(
                "OpenTelemetry is enabled but the OTLP HTTP exporter is not installed. "
                "Spans will be created but not exported."
            )

    # ``trace.set_tracer_provider`` is set-once-per-process; calling it
    # twice (e.g. across multiple test invocations) silently keeps the
    # first provider and emits a warning, which would make the second
    # ``trace.get_tracer`` return a tracer attached to a stale
    # provider. Get the tracer off our own provider instead — the
    # TracerHandle is self-contained, and we still register the
    # provider globally on the *first* call so third-party libraries
    # that call ``trace.get_tracer`` directly pick it up.
    if trace.get_tracer_provider().__class__.__name__ in {"ProxyTracerProvider", "NoOpTracerProvider"}:
        trace.set_tracer_provider(provider)
    tracer = provider.get_tracer(_TRACER_NAME)
    return TracerHandle(tracer, provider)


def _otlp_endpoint_configured() -> bool:
    """Has the user set an OTLP endpoint via env (otherwise: don't export)?"""
    return any(
        os.environ.get(name)
        for name in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        )
    )


def attach_span_exporter(handle: TracerHandle, exporter: Any) -> None:
    """Test hook: wire an arbitrary :class:`SpanExporter` into a tracer handle.

    The :class:`opentelemetry.sdk.trace.export.SimpleSpanProcessor` is
    used so spans land in the exporter synchronously — matches what
    tests want when asserting on captured spans.
    """
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    handle._provider.add_span_processor(SimpleSpanProcessor(exporter))


@contextlib.contextmanager
def tool_span(
    handle: TracerHandle | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> Iterator[Any]:
    """Yield a span (or ``None``) for one MCP tool call.

    A unified entry point so the server can wrap every ``call_tool``
    in ``with tool_span(...)`` regardless of whether OTel is enabled.
    """
    if handle is None:
        yield None
        return
    with handle.tool_span(tool_name, arguments) as span:
        yield span
