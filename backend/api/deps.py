"""Shared helpers for API endpoints.

All helpers extract runtime resources from `request.app.state` (set in
main.py lifespan) instead of using module-level globals.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request
from langchain_core.messages import HumanMessage

from backend.core.graph import AgentRuntime
from backend.core.schema import BRDResponse


def _config(thread_id: str) -> dict:
    """The RunnableConfig handed to every graph invocation."""
    return {"configurable": {"thread_id": thread_id}}


def _read_graph_state(request: Request, thread_id: str) -> dict:
    """Pull the current persisted state for a thread (empty dict if none)."""
    graph = request.app.state.gathering_graph
    snapshot = graph.get_state(_config(thread_id))
    return snapshot.values if snapshot else {}


def _turn_id(request: Request, thread_id: str) -> int:
    """Best-effort monotonic turn id derived from #HumanMessages in state."""
    state = _read_graph_state(request, thread_id)
    messages = state.get("messages") or []
    return sum(1 for m in messages if isinstance(m, HumanMessage))


def _current_draft(request: Request, thread_id: str) -> Optional[BRDResponse]:
    state = _read_graph_state(request, thread_id)
    raw = state.get("current_draft")
    return BRDResponse.model_validate(raw) if raw else None


def get_runtime(request: Request) -> AgentRuntime:
    if not hasattr(request.app.state, "runtime") or request.app.state.runtime is None:
        raise RuntimeError("Agent runtime not initialized — lifespan didn't run")
    return request.app.state.runtime
