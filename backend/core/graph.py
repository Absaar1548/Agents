"""LangGraph definition for the BRD agent.

ChatbotState is the per-thread state persisted by the SqliteSaver
checkpointer. main.py invokes one of two compiled graphs per
endpoint:

  gathering_graph  →  POST /chat, POST /request-changes
                       retrieve_context → invoke_llm → extract_memory → END
                       Adds the user's HumanMessage + an AIMessage to
                       state.messages, updates state.brd_memory.

  drafting_graph   →  POST /generate-brd
                       retrieve_context → draft_llm → schema_validate → END
                       Does NOT add to state.messages; produces
                       state.current_draft (a BRDResponse JSON).

Telemetry: each node opens its own `chat_span` with the right
openinference.span.kind. The Phase 1 root `brd_agent.turn` / `brd_agent.draft`
span lives in main.py — wrapped around `graph.invoke(...)`. The LangChain
auto-instrumentor is intentionally NOT activated here; we evaluate that in
Phase 6 to avoid duplicate span tree clutter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend.artifacts import ArtifactStore
from backend.context.assembler import ContextAssembler
from backend.core.state import ChatbotState
from backend.llm import BaseLLMClient


@dataclass
class AgentRuntime:
    """Dependencies injected into every node via closure capture at compile time.

    Beats threading the LangGraph RunnableConfig.configurable through every
    node — these are construction-time singletons, not per-turn config.
    """

    llm: BaseLLMClient
    memory_manager: Any            # langmem.MemoryStoreManager
    store: Any                     # langgraph.store.base.BaseStore
    assembler: ContextAssembler    # stateless; one instance per process
    artifact_store: ArtifactStore  # large tool outputs live here (Phase 5)


def _bind(fn, runtime: AgentRuntime):
    """Bind `runtime` into a node fn while keeping the LangGraph-canonical
    `(state, config)` signature so LangGraph's introspection injects the
    RunnableConfig under the `config` name."""

    def node(state, config):
        return fn(state, config, runtime)

    node.__name__ = fn.__name__
    return node


def build_gathering_graph(runtime: AgentRuntime, checkpointer):
    """gathering_graph: handles POST /chat and POST /request-changes.

    `state.mode` distinguishes which prompt set + assembler strategy each
    node uses. Same wiring; different content per mode.
    """
    from backend.nodes.extract_memory import extract_memory
    from backend.nodes.invoke_llm import invoke_llm
    from backend.nodes.retrieve_context import retrieve_context
    from backend.nodes.summarize import summarize

    g = StateGraph(ChatbotState)
    g.add_node("summarize", _bind(summarize, runtime))
    g.add_node("retrieve_context", _bind(retrieve_context, runtime))
    g.add_node("invoke_llm", _bind(invoke_llm, runtime))
    g.add_node("extract_memory", _bind(extract_memory, runtime))
    g.add_edge(START, "summarize")
    g.add_edge("summarize", "retrieve_context")
    g.add_edge("retrieve_context", "invoke_llm")
    g.add_edge("invoke_llm", "extract_memory")
    g.add_edge("extract_memory", END)
    return g.compile(checkpointer=checkpointer)


def build_drafting_graph(runtime: AgentRuntime, checkpointer):
    """drafting_graph: handles POST /generate-brd.

    Reads existing thread state (messages + brd_memory) and produces a
    BRDResponse. Does not extend messages or update memory.
    """
    from backend.nodes.draft import draft_llm
    from backend.nodes.retrieve_context import retrieve_context
    from backend.nodes.schema_validate import schema_validate

    g = StateGraph(ChatbotState)
    g.add_node("retrieve_context", _bind(retrieve_context, runtime))
    g.add_node("draft_llm", _bind(draft_llm, runtime))
    g.add_node("schema_validate", _bind(schema_validate, runtime))
    g.add_edge(START, "retrieve_context")
    g.add_edge("retrieve_context", "draft_llm")
    g.add_edge("draft_llm", "schema_validate")
    g.add_edge("schema_validate", END)
    return g.compile(checkpointer=checkpointer)
