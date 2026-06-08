"""summarize node — rolling-summary trim policy.

Runs first in the gathering graph. No-op when the transcript is short.
When len(state.messages) > TRIM_THRESHOLD:
  1. Take the OLDEST slice (everything except the last KEEP_TAIL turns).
  2. Call Azure with SUMMARIZE_SYSTEM_PROMPT to produce a structured
     ConversationSummary JSON.
  3. Merge with state.rolling_summary (if any) — list-union + narrative
     concatenation.
  4. Emit RemoveMessage instances to drop those old turns from state.
  5. Return the merged summary + RemoveMessage list.

The summary itself lives in state.rolling_summary (a dict matching
ConversationSummary). The assembler reads it on subsequent turns.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState
from backend.context.prompts import (
    SUMMARIZE_PROMPT_HASH,
    SUMMARIZE_PROMPT_ID,
    SUMMARIZE_PROMPT_VERSION,
    SUMMARIZE_SYSTEM_PROMPT,
)
from backend.telemetry import KIND_CHAIN, chat_span

TRIM_THRESHOLD = 12   # summarize once messages cross this count
KEEP_TAIL = 8         # always keep the most recent KEEP_TAIL turns raw

def _transcript(messages: list[BaseMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"USER: {m.content}")
        elif isinstance(m, AIMessage):
            lines.append(f"ASSISTANT: {m.content}")
    return "\n".join(lines)


def _merge_summary(
    existing: Optional[dict], new: dict
) -> dict:
    """Merge a freshly produced ConversationSummary with any pre-existing one.

    Lists union with naive dedupe; compressed_narrative concatenates with
    a paragraph break.
    """
    if not existing:
        return new
    out = dict(existing)
    for key in ("recent_topics", "decisions_so_far", "pending_clarifications"):
        seen = list(out.get(key) or [])
        for x in new.get(key) or []:
            if x not in seen:
                seen.append(x)
        out[key] = seen
    if new.get("compressed_narrative"):
        prior = out.get("compressed_narrative") or ""
        out["compressed_narrative"] = (
            f"{prior}\n\n{new['compressed_narrative']}".strip()
        )
    return out


def summarize(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]
    messages = state.get("messages") or []

    with chat_span(
        "summarize",
        session_id=session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("summarize.messages_count", len(messages))
        span.set_attribute("summarize.threshold", TRIM_THRESHOLD)
        span.set_attribute("summarize.keep_tail", KEEP_TAIL)

        if len(messages) <= TRIM_THRESHOLD:
            span.set_attribute("summarize.triggered", False)
            return {}

        # Slice to summarize: everything before the tail we want to keep raw
        to_summarize = list(messages[:-KEEP_TAIL])
        # IDs to remove — LangGraph requires the id to identify which message
        remove_ids = [m.id for m in to_summarize if m.id is not None]

        span.set_attribute("summarize.triggered", True)
        span.set_attribute("summarize.slice_count", len(to_summarize))
        span.set_attribute("summarize.remove_count", len(remove_ids))
        span.set_attribute("prompt.id", SUMMARIZE_PROMPT_ID)
        span.set_attribute("prompt.version", SUMMARIZE_PROMPT_VERSION)
        span.set_attribute("prompt.template_hash", SUMMARIZE_PROMPT_HASH)
        span.set_attribute("llm.provider", runtime.llm.provider)
        span.set_attribute("llm.model", runtime.llm.model)

        transcript_text = _transcript(to_summarize)
        raw = runtime.llm.complete(
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Summarize this older slice of conversation per the schema:\n\n"
                        + transcript_text
                    ),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=600,
            temperature=0.0,
        )

        try:
            new_summary = json.loads(raw)
        except Exception:
            span.add_event(
                "summary_parse_error",
                attributes={"raw_snippet": raw[:500]},
            )
            return {}

        merged = _merge_summary(state.get("rolling_summary"), new_summary)
        span.set_attribute(
            "summary.recent_topics_count", len(merged.get("recent_topics") or [])
        )
        span.set_attribute(
            "summary.decisions_count", len(merged.get("decisions_so_far") or [])
        )
        span.set_attribute(
            "summary.narrative_chars",
            len(merged.get("compressed_narrative") or ""),
        )

        return {
            "rolling_summary": merged,
            "messages": [RemoveMessage(id=mid) for mid in remove_ids],
        }
