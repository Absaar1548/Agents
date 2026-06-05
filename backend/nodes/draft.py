"""draft_llm — Phase 2: consumes assembled messages from state.

retrieve_context (called first in the drafting graph) infers strategy
BRD_GENERATION (because state.mode == "drafting") and uses the drafting
system prompt. The assembled message list lands in
state.last_retrievals["assembled"]["messages"]; this node calls Azure
with json_object response_format and stashes the raw JSON for
schema_validate.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.graph import AgentRuntime, ChatbotState
from backend.telemetry import KIND_CHAIN, chat_span

def draft_llm(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]
    last_ret = state.get("last_retrievals") or {}
    assembled = last_ret.get("assembled") or {}
    messages = assembled.get("messages") or []

    with chat_span(
        "draft_llm",
        session_id=session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.INTERNAL,
        chat_mode="drafting",
    ) as span:
        span.set_attribute("prompt.id", assembled.get("prompt_id", ""))
        span.set_attribute("prompt.version", assembled.get("prompt_version", ""))
        span.set_attribute(
            "prompt.template_hash", assembled.get("prompt_template_hash", "")
        )
        span.set_attribute("llm.provider", runtime.llm.provider)
        span.set_attribute("llm.model", runtime.llm.model)
        span.set_attribute("llm.response_format", "json_object")
        span.set_attribute("llm.max_tokens", 4000)
        span.set_attribute("messages.count", len(messages))

        raw = runtime.llm.complete(
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=4000,
            temperature=0.2,
        )

        return {"last_retrievals": {**last_ret, "draft_raw": raw}}
