"""retrieve_context — Phase 2: calls ContextAssembler.assemble.

Inputs read from state: messages, mode, brd_memory, rolling_summary,
current_draft. Strategy is inferred from mode + the latest user message
+ whether a draft exists.

Writes state.last_retrievals = {
    "strategy": <ContextStrategy value>,
    "messages": <OpenAI dicts ready for LLM>,
    "token_accounting": <per-section token counts>,
    "dropped_sections": [...],
    "provenance": {...},
    "prompt_id": "...",
    "prompt_version": "...",
    "prompt_template_hash": "...",
}

invoke_llm and draft_llm read state.last_retrievals["messages"] and pass
it straight to the Azure client — no more build_messages() at the LLM nodes.
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.graph import AgentRuntime, ChatbotState
from backend.strategy import infer_strategy
from backend.telemetry import KIND_CHAIN, add_memory_event, chat_span


def _last_user_text(messages: list[BaseMessage]) -> Optional[str]:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content
    return None


def retrieve_context(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]
    mode = state.get("mode", "gathering")
    messages = state.get("messages") or []
    brd_memory = state.get("brd_memory")
    rolling_summary = state.get("rolling_summary")
    current_draft = state.get("current_draft")

    user_msg = _last_user_text(messages)
    strategy = infer_strategy(
        mode=mode,
        user_message=user_msg,
        has_draft=current_draft is not None,
    )

    with chat_span(
        "retrieve_context",
        session_id=session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.INTERNAL,
        chat_mode=mode,
    ) as span:
        span.set_attribute("retrieve.strategy", strategy.value)
        span.set_attribute("retrieve.messages_count", len(messages))
        span.set_attribute("retrieve.has_memory", brd_memory is not None)
        span.set_attribute("retrieve.has_summary", rolling_summary is not None)
        span.set_attribute("retrieve.has_draft", current_draft is not None)

        # For REVIEW_FEEDBACK / REFINEMENT, the feedback is the last user message.
        feedback = user_msg if mode == "request_changes" else None

        # Drop memory snapshot as an event for drill-down debugging.
        add_memory_event(span, brd_memory)

        last_ret = state.get("last_retrievals") or {}
        artifact_refs = last_ret.get("artifact_refs") or []

        assembled = runtime.assembler.assemble(
            strategy=strategy,
            session_id=session_id,
            messages_in_state=messages,
            brd_memory=brd_memory,
            rolling_summary=rolling_summary,
            current_draft=current_draft,
            feedback=feedback,
            artifact_refs=artifact_refs,
        )

        # Surface token accounting + prompt lineage on this span (the LLM
        # nodes will set llm.* on their own spans).
        acct = assembled.token_accounting
        span.set_attribute("prompt.tokens.system_prompt", acct.system_prompt)
        span.set_attribute("prompt.tokens.memory", acct.memory)
        span.set_attribute("prompt.tokens.summary", acct.summary)
        span.set_attribute("prompt.tokens.draft_block", acct.draft_block)
        span.set_attribute("prompt.tokens.docs", acct.docs)
        span.set_attribute("prompt.tokens.kg", acct.kg)
        span.set_attribute("prompt.tokens.artifacts", acct.artifacts)
        span.set_attribute("prompt.tokens.conversation", acct.conversation)
        span.set_attribute("retrieve.artifact_refs_count", len(artifact_refs))
        span.set_attribute("prompt.tokens.total", acct.total)
        span.set_attribute("prompt.id", assembled.prompt_id)
        span.set_attribute("prompt.version", assembled.prompt_version)
        span.set_attribute("prompt.template_hash", assembled.prompt_template_hash)
        span.set_attribute("messages.count", len(assembled.messages))
        if assembled.dropped_sections:
            span.set_attribute(
                "context.dropped_sections", ",".join(assembled.dropped_sections)
            )

        existing = state.get("last_retrievals") or {}
        return {
            "last_retrievals": {
                **existing,
                "assembled": assembled.model_dump(mode="json"),
            }
        }
