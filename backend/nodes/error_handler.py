"""error_handler node — Phase 2 implementation.

Called when schema validation fails after max retries.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState


def error_handler(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    """Graceful degradation after max schema validation retries exceeded."""
    errors = state.get("validation_errors") or []
    reply = (
        "Unable to produce a valid BRD after 2 retries. "
        f"Validation errors: {'; '.join(errors)}. "
        "Please try again or adjust your requirements."
    )
    return {"reply_text": reply}
