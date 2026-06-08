"""schema_validate node — parses draft JSON and validates against BRDResponse.

On success: writes the BRDResponse dump into state.current_draft.
On failure: sets retry_count + validation_errors so the drafting graph can
loop back to draft_llm (max 2 retries) before routing to error_handler.
"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.core.graph import AgentRuntime
from backend.core.schema import BRDResponse
from backend.core.state import ChatbotState, DraftStatus
from backend.guardrails.presidio import scan_dict_strings
from backend.telemetry import KIND_TOOL, chat_span

DRAFTED_BY = "brd-agent@0.1.0"


def _extract_json(raw: str) -> str:
    """Try to extract a JSON object from raw LLM output.

    Some models (especially via Ollama) ignore response_format=json_object
    when the conversation context is long and return plain text or wrap the
    JSON in markdown fences.  This helper:
      1. Strips ```json ... ``` or ``` ... ``` fences.
      2. Falls back to the first {...} block found in the text.
      3. Returns the original string if no fences/blocks are detected.
    """
    stripped = raw.strip()

    # 1. Markdown code fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1)

    # 2. First top-level JSON object {...}
    obj_match = re.search(r"(\{.*\})", stripped, re.DOTALL)
    if obj_match:
        return obj_match.group(1)

    # 3. Nothing detected — return as-is and let json.loads fail clearly
    return stripped


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
            candidate = _extract_json(raw)
            data = json.loads(candidate)
            data["drafted_by"] = DRAFTED_BY
            brd = BRDResponse.model_validate(data)
            span.set_attribute("schema.outcome", "valid")
            span.set_attribute("schema.error_count", 0)
            span.set_attribute("schema.fr_count", len(brd.functional_requirements))
            span.set_attribute("schema.nfr_count", len(brd.non_functional_requirements))
            span.set_attribute("schema.risk_count", len(brd.risks))

            # Point 4: scan generated BRD before it reaches user / reviewer
            cleaned_draft, masked, logged = scan_dict_strings(
                brd.model_dump(mode="json"),
                session_id=session_id,
                scan_name="guardrails.schema_validate.output",
            )
            span.set_attribute("guardrail.output.masked_count", len(masked))
            span.set_attribute("guardrail.output.logged_count", len(logged))

            # Success — return cleaned current_draft and reset retry counters
            return {
                "current_draft": cleaned_draft,
                "draft_status": DraftStatus.DRAFT,
                "retry_count": 0,
                "validation_errors": [],
            }
        except Exception as exc:
            span.set_attribute("schema.outcome", "invalid")
            retry_count = (state.get("retry_count") or 0) + 1
            errors = [str(exc)]
            span.set_attribute("schema.error_count", len(errors))
            span.set_attribute("schema.retry_count", retry_count)
            span.add_event("raw_snippet", attributes={"raw": raw[:500]})

            # Failure — return retry state for conditional edge routing
            return {
                "retry_count": retry_count,
                "validation_errors": errors,
            }
