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
    """Stub — Phase 2 will implement max-retry error handling."""
    return {}
