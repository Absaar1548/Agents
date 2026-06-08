"""invoke_llm — Phase 2: consumes assembled messages from state.

The assembler (called in retrieve_context) wrote the final OpenAI-format
messages list into state.last_retrievals["assembled"]["messages"]. This
node just calls Azure with them and appends the AIMessage reply.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState
from backend.telemetry import KIND_CHAIN, chat_span

READY_MARKER = "[READY_FOR_PRODUCTION]"


def invoke_llm(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]
    mode = state.get("mode", "gathering")
    last_ret = state.get("last_retrievals") or {}
    assembled = last_ret.get("assembled") or {}
    messages = assembled.get("messages") or []

    with chat_span(
        "invoke_llm",
        session_id=session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.INTERNAL,
        chat_mode=mode,
    ) as span:
        # Prompt lineage already on retrieve_context; we still tag it here
        # so the LLM-call span carries it per the trace contract.
        span.set_attribute("prompt.id", assembled.get("prompt_id", ""))
        span.set_attribute("prompt.version", assembled.get("prompt_version", ""))
        span.set_attribute(
            "prompt.template_hash", assembled.get("prompt_template_hash", "")
        )
        span.set_attribute("llm.provider", runtime.llm.provider)
        span.set_attribute("llm.model", runtime.llm.model)
        span.set_attribute("llm.temperature", 0.4)
        span.set_attribute("llm.max_tokens", 800)
        span.set_attribute("messages.count", len(messages))

        reply = runtime.llm.complete(
            messages=messages, temperature=0.4, max_tokens=800
        )

        # Phase 2: detect readiness marker
        ready = READY_MARKER in reply
        clean_reply = reply.replace(READY_MARKER, "").strip()
        if ready:
            span.set_attribute("hitl.ready_for_production", True)

        return {
            "messages": [AIMessage(content=clean_reply)],
            "reply_text": clean_reply,
            "ready_for_production": ready,
        }
