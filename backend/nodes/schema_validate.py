"""schema_validate node — parses draft JSON and validates against BRDResponse.

On success: writes the BRDResponse dump into state.current_draft.
On failure: span is marked ERROR (via chat_span's exception path) and the
error propagates — main.py's `chat_span` wrapping graph.invoke catches it
and surfaces a 500 to the client.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.graph import AgentRuntime, ChatbotState
from backend.schema import BRDResponse
from backend.telemetry import KIND_TOOL, chat_span

DRAFTED_BY = "brd-agent@0.1.0"


def schema_validate(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]
    raw = (state.get("last_retrievals") or {}).get("draft_raw", "")

    with chat_span(
        "schema_validate",
        session_id=session_id,
        span_kind=KIND_TOOL,
        otel_kind=SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("schema.id", "BRDResponse")
        try:
            data = json.loads(raw)
            data["drafted_by"] = DRAFTED_BY
            brd = BRDResponse.model_validate(data)
            span.set_attribute("schema.outcome", "valid")
            span.set_attribute("schema.error_count", 0)
            span.set_attribute("schema.fr_count", len(brd.functional_requirements))
            span.set_attribute("schema.nfr_count", len(brd.non_functional_requirements))
            span.set_attribute("schema.risk_count", len(brd.risks))
        except Exception:
            span.set_attribute("schema.outcome", "invalid")
            span.set_attribute("schema.error_count", 1)
            span.add_event("raw_snippet", attributes={"raw": raw[:500]})
            raise

        return {"current_draft": brd.model_dump(mode="json")}
