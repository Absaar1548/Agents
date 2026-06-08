"""extract_memory node — LangMem extracts BRDMemory from the last exchange.

Runs after invoke_llm appended the AIMessage. Sends the last two messages
(the user turn + the assistant turn) to the LangMem manager, then reads
the updated memory back from the store and returns it as a state update.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.trace import SpanKind

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState
from backend.memory.manager import manager_config, read_memory
from backend.telemetry import KIND_CHAIN, chat_span


def _last_exchange(messages: list[BaseMessage]) -> list[dict]:
    """Return the last user→assistant pair in OpenAI dict format."""
    out: list[dict] = []
    for m in messages[-2:]:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": m.content})
    return out


def extract_memory(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    session_id = config["configurable"]["thread_id"]

    with chat_span(
        "extract_memory",
        session_id=session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("memory.schema", "BRDMemory")
        last = _last_exchange(state.get("messages", []))
        span.set_attribute("memory.input_messages_count", len(last))

        if not last:
            return {}

        result = runtime.memory_manager.invoke(
            {"messages": last},
            config=manager_config(session_id),
        )
        span.set_attribute(
            "memory.items_after",
            len(result) if isinstance(result, list) else 0,
        )

        new_memory = read_memory(runtime.store, session_id)
        return {"brd_memory": new_memory}
