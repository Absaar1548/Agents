"""hitl_gate node — Phase 2 implementation.

Uses LangGraph interrupt() to pause before drafting.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from backend.core.graph import AgentRuntime
from backend.core.state import ChatbotState


def hitl_gate(
    state: ChatbotState,
    config: RunnableConfig,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    """HITL 1 — pause the graph and present a summary for user confirmation.

    The graph resumes when the client sends Command(resume=action).
    """
    brd_memory = state.get("brd_memory") or {}
    pending_feedback = state.get("pending_feedback") or []

    # Build a concise summary from captured memory
    summary = {
        "title": brd_memory.get("title", "Untitled"),
        "objectives": brd_memory.get("objectives", []),
        "open_questions": brd_memory.get("open_questions", []),
        "feedback_items": len(pending_feedback),
    }

    # LangGraph interrupt — pauses graph, persists state to SqliteSaver
    action = interrupt({
        "type": "hitl_1",
        "summary": summary,
        "actions": ["proceed", "add_more"],
    })

    # action is the resume value from Command(resume=...)
    if action == "proceed":
        return {
            "mode": "drafting",
            "feedback_gathering": False,
            "ready_for_production": False,
        }
    # "add_more" or any other value
    return {
        "mode": "gathering",
        "feedback_gathering": True,
        "ready_for_production": False,
    }
