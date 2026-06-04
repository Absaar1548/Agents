"""OpenTelemetry setup + chat_span helper.

OTel is the source of truth for what happens on each turn. Every meaningful
step opens a span via chat_span(); every LLM call gets an auto-instrumented
child span via OpenInference's OpenAIInstrumentor. The intent is that a
Phoenix trace alone is sufficient to reconstruct any turn.

Span contract (applied uniformly by chat_span):

  Resource:
    service.name      = "brd-agent" (or $OTEL_SERVICE_NAME)
    service.version   = SERVICE_VERSION
    service.namespace = "brd-poc"      ← groups brd_agent + ws8_chassis in Phoenix
    deployment.environment = "local" (or $DEPLOYMENT_ENVIRONMENT)

  Universal span attributes (every span):
    session.id            (OpenInference SpanAttributes.SESSION_ID)
    user.id               (synthetic placeholder, SpanAttributes.USER_ID)
    agent.id, agent.version
    principal.tenant_id, principal.user_oid_hash
    openinference.span.kind  ∈ {AGENT, CHAIN, TOOL, RETRIEVER}
    turn.id (if provided), chat.mode (if provided), tag.tags (if provided)

  Root spans additionally carry:
    input.value / output.value (text/plain or application/json)
    agent.outcome (set by caller on success path; chat_span sets "error" on except)

Phoenix listens on http://localhost:6006/v1/traces (OTLP/HTTP). Phoenix's
own gRPC port (default 4317) is collided in this env; that doesn't affect
trace delivery because we use HTTP, but Phoenix itself needs a free gRPC
port to start. Use `phoenix serve --grpc-port 4347` if relaunching.
"""
from __future__ import annotations

import hashlib
import json as _json
import os
from contextlib import contextmanager
from typing import Iterator, Optional

from openinference.instrumentation.openai import OpenAIInstrumentor
from openinference.semconv.trace import (
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer


SERVICE_NAME = "brd-agent"
SERVICE_VERSION = "0.1.0"
SERVICE_NAMESPACE = "brd-poc"
AGENT_ID = "brd-agent"
DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006/v1/traces"

# Synthetic principal — placeholder until WS#3 attaches a real one. Hashed
# so it conforms to the trace-contract rule "never raw OIDs in spans".
_PRINCIPAL_TENANT_ID = "northern-trust"
_PRINCIPAL_USER_OID = "synthetic-poc-user"
_PRINCIPAL_USER_OID_HASH = hashlib.sha256(_PRINCIPAL_USER_OID.encode()).hexdigest()
_SYNTHETIC_USER_ID = _PRINCIPAL_USER_OID  # same identity, exposed via OpenInference SpanAttributes.USER_ID

# Span-kind constants for convenience at call sites.
KIND_AGENT = OpenInferenceSpanKindValues.AGENT.value
KIND_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
KIND_TOOL = OpenInferenceSpanKindValues.TOOL.value
KIND_RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value

_initialized: bool = False
_provider: Optional[TracerProvider] = None


def init_telemetry(
    endpoint: Optional[str] = None,
    service_name: Optional[str] = None,
    deployment_environment: Optional[str] = None,
    enable_auto_instrumentation: bool = True,
) -> None:
    """Configure OTel + Phoenix exporter + OpenAI auto-instrumentation.

    Must be called BEFORE constructing the OpenAI client (the instrumentor
    patches the SDK at import time).
    """
    global _initialized, _provider
    if _initialized:
        return

    endpoint = endpoint or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_PHOENIX_ENDPOINT
    )
    service_name = service_name or os.environ.get("OTEL_SERVICE_NAME", SERVICE_NAME)
    deployment_environment = deployment_environment or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": SERVICE_NAMESPACE,
            "service.version": SERVICE_VERSION,
            "deployment.environment": deployment_environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(provider)
    _provider = provider

    if enable_auto_instrumentation:
        OpenAIInstrumentor().instrument()

    _initialized = True


def get_tracer(name: str = "brd_agent") -> Tracer:
    return trace.get_tracer(name)


def flush_telemetry(timeout_millis: int = 5000) -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=timeout_millis)


@contextmanager
def chat_span(
    name: str,
    *,
    session_id: str,
    span_kind: str = KIND_CHAIN,
    otel_kind: SpanKind = SpanKind.INTERNAL,
    turn_id: str | int | None = None,
    chat_mode: Optional[str] = None,
    tags: Optional[list[str]] = None,
    track_outcome: bool = False,
    agent_id: str = AGENT_ID,
    agent_version: str = SERVICE_VERSION,
) -> Iterator[Span]:
    """Open a span with the universal-attribute contract attached.

    Args:
        name: span name in lowercase dot-notation (e.g. "brd_agent.turn").
        session_id: logical session UUID; carried as openinference session.id.
        span_kind: OpenInference span kind for Phoenix rendering. Use the
            KIND_* constants (AGENT for root agentic actions, CHAIN for
            internal steps, RETRIEVER for memory reads, TOOL for validation).
        otel_kind: OTel SpanKind. SERVER for spans wrapping a user-triggered
            request handler; INTERNAL for everything else.
        turn_id: monotonic turn number, or "draft" / "approve" / "reset".
        chat_mode: "gathering" | "drafting" | "request_changes" | "approve" | "reset".
        tags: list of free-form tags exposed as openinference tag.tags
            (Phoenix shows these as chips and offers filter-by-tag).
        track_outcome: when True, on exception the span gets
            agent.outcome="error" automatically (callers can override or
            set richer values like "schema_error" before raising).
        agent_id, agent_version: identity attached as universal attrs.

    On exception:
      - record_exception(e) attaches the exception structurally,
      - status is set to ERROR with the exception message,
      - error.kind is set to the exception class name,
      - if track_outcome=True, agent.outcome="error".
    On clean exit:
      - status is set explicitly to OK so Phoenix renders it.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=otel_kind) as span:
        # OpenInference universal attrs (drives Phoenix UI columns + filters).
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, span_kind)
        span.set_attribute(SpanAttributes.SESSION_ID, session_id)
        span.set_attribute(SpanAttributes.USER_ID, _SYNTHETIC_USER_ID)

        # Chassis universal attrs (matches ws8_chassis trace contract §6.2).
        span.set_attribute("session.id", session_id)
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("agent.version", agent_version)
        span.set_attribute("principal.tenant_id", _PRINCIPAL_TENANT_ID)
        span.set_attribute("principal.user_oid_hash", _PRINCIPAL_USER_OID_HASH)

        if turn_id is not None:
            span.set_attribute("turn.id", str(turn_id))
        if chat_mode is not None:
            span.set_attribute("chat.mode", chat_mode)
        if tags:
            span.set_attribute(SpanAttributes.TAG_TAGS, tags)

        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.set_attribute("error.kind", type(e).__name__)
            if track_outcome:
                span.set_attribute("agent.outcome", "error")
            raise
        else:
            span.set_status(Status(StatusCode.OK))


# ----- small attribute helpers ----------------------------------------

_MAX_INPUT_OUTPUT_LEN = 8000  # keep span size sane


def set_input(
    span: Span,
    value: object,
    mime: str = "text/plain",
) -> None:
    """Attach input.value / input.mime_type to a root span."""
    s = value if isinstance(value, str) else _json.dumps(value, default=str)
    span.set_attribute(SpanAttributes.INPUT_VALUE, s[:_MAX_INPUT_OUTPUT_LEN])
    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, mime)


def set_output(
    span: Span,
    value: object,
    mime: str = "text/plain",
) -> None:
    """Attach output.value / output.mime_type to a root span."""
    s = value if isinstance(value, str) else _json.dumps(value, default=str)
    span.set_attribute(SpanAttributes.OUTPUT_VALUE, s[:_MAX_INPUT_OUTPUT_LEN])
    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, mime)


def set_outcome(span: Span, outcome: str) -> None:
    """Set agent.outcome on the root span. Call on the success path; the
    chat_span helper handles the error path automatically when
    track_outcome=True."""
    span.set_attribute("agent.outcome", outcome)


@contextmanager
def source_span(
    name: str,
    *,
    session_id: str,
    source_kind: str,
    query: Optional[str] = None,
    k: Optional[int] = None,
    focus: Optional[str] = None,
    turn_id: str | int | None = None,
) -> Iterator[Span]:
    """Open a uniform retrieval-source span.

    Use for any layer that the assembler queries (Vector DB, KG, future
    artifact-store lookups, etc.). Encodes the trace contract for sources:

      - openinference.span.kind = RETRIEVER
      - source.kind             — short identifier (vector_store, kg, ...)
      - source.query            — the query string used (truncated to 200)
      - source.k                — caller-requested hit count
      - source.focus            — advisory filter / strategy hint (optional)
      - source.duration_ms      — set automatically on exit

    Callers still set source.hits_count + source.hit_ids inside the block;
    those depend on the result. The contract for what hits_count means is
    documented in test/trace_contract.md.
    """
    import time
    start = time.perf_counter()
    with chat_span(
        name,
        session_id=session_id,
        span_kind=KIND_RETRIEVER,
        otel_kind=trace.SpanKind.INTERNAL,
        turn_id=turn_id,
    ) as span:
        span.set_attribute("source.kind", source_kind)
        if query is not None:
            span.set_attribute("source.query", (query or "")[:200])
        if k is not None:
            span.set_attribute("source.k", k)
        if focus is not None:
            span.set_attribute("source.focus", focus)
        try:
            yield span
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            span.set_attribute("source.duration_ms", round(elapsed_ms, 2))


def add_memory_event(span: Span, memory: Optional[dict]) -> None:
    """Attach the BRDMemory snapshot as a span event (truncated). Drill-down
    detail — small enough to fit, large enough to debug 'what did the agent
    know at this turn?' from the trace alone."""
    if not memory:
        span.add_event("memory_snapshot", attributes={"memory.is_empty": True})
        return
    snapshot = _json.dumps(memory, default=str)[:_MAX_INPUT_OUTPUT_LEN]
    span.add_event(
        "memory_snapshot",
        attributes={
            "memory.is_empty": False,
            "memory.snapshot": snapshot,
            "memory.snapshot.truncated": len(_json.dumps(memory, default=str))
            > _MAX_INPUT_OUTPUT_LEN,
        },
    )
