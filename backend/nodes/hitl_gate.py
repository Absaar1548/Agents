"""hitl_gate node — Phase 2 implementation.

Uses LangGraph interrupt() to pause before drafting.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState


def hitl_gate(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    """Stub — Phase 2 will implement interrupt() here."""
    return {}
