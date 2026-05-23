"""OpenTelemetry tracer bootstrap.

A thin helper around the SDK that picks the exporter based on
``Config.otel_exporter`` and returns the configured
:class:`TracerProvider`. Tests pass their own provider (with an
in-memory exporter) so this module is only the production path.
"""
from __future__ import annotations

from typing import Literal

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

ExporterKind = Literal["grpc", "http", "console"]


def _make_exporter(
    kind: ExporterKind, endpoint: str | None
) -> SpanExporter:
    if kind == "console":
        return ConsoleSpanExporter()
    if kind == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )

        return GrpcExporter(endpoint=endpoint) if endpoint else GrpcExporter()
    if kind == "http":
        # Optional dep — surfaced as ImportError when the user enabled HTTP
        # but didn't install the optional package.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found, unused-ignore]
            OTLPSpanExporter as HttpExporter,
        )

        exp: SpanExporter = HttpExporter(endpoint=endpoint) if endpoint else HttpExporter()
        return exp
    raise ValueError(f"unknown exporter kind: {kind!r}")


def setup_tracer(
    *,
    endpoint: str | None = None,
    exporter: ExporterKind = "grpc",
    service_name: str = "gg-relay",
    install_global: bool = True,
) -> TracerProvider:
    """Build a :class:`TracerProvider` with one batch processor + exporter.

    Installs the provider as the global default unless
    ``install_global=False``; tests pass ``install_global=False`` so they
    don't poison other test modules.
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(_make_exporter(exporter, endpoint)))
    if install_global:
        trace.set_tracer_provider(provider)
    return provider
