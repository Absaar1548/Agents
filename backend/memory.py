"""LangMem-based memory.

Two schemas, one manager, one in-memory store, one namespace.

  BRDMemory             — structured working memory of BRD facts
                          (stakeholders, FRs, NFRs, constraints, ...).
  ConversationSummary   — rolling summary of OLD trimmed turns
                          (recent_topics, decisions_so_far, ...).
                          Populated by the summarize node only when turns
                          get trimmed; gathering turns don't re-summarize.

Both live in the same namespace (`brd_agent/<session_id>`). Schemas have
disjoint field sets, so read_memory and read_summary can discriminate by
inspecting the value's field signature.

Design choices:
  - InMemoryStore() without an index — no embeddings, no semantic search.
  - enable_inserts=True, enable_deletes=False — manager appends; we never
    let it forget mid-conversation.

The LangMem manager calls go through langchain-openai → openai SDK, so
OpenInference's openai instrumentor picks them up automatically as LLM
spans under whichever node invokes the manager.
"""
from __future__ import annotations

import os
from typing import Optional

from langchain_openai import AzureChatOpenAI
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langmem import create_memory_store_manager
from pydantic import BaseModel, Field


# ----- BRDMemory schema -----
class StakeholderMemo(BaseModel):
    name: str
    role: str | None = None
    interest: str | None = None


class FRMemo(BaseModel):
    title: str
    description: str | None = None
    priority: str | None = None  # low | medium | high | critical (free-form here)


class NFRMemo(BaseModel):
    category: str | None = None  # performance | security | ...
    description: str
    target: str | None = None


class BRDMemory(BaseModel):
    """Working memory of BRD info gathered across the conversation."""

    title: str | None = None
    background_notes: str | None = None
    objectives: list[str] = Field(default_factory=list)
    stakeholders: list[StakeholderMemo] = Field(default_factory=list)
    functional_requirements: list[FRMemo] = Field(default_factory=list)
    non_functional_requirements: list[NFRMemo] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ----- ConversationSummary schema -----
# Field names are intentionally DISJOINT from BRDMemory so we can
# discriminate read_summary results by field signature.
class ConversationSummary(BaseModel):
    """Rolling summary of older, trimmed conversation turns.

    Populated by the summarize node when the raw transcript grows past
    the trim threshold. Older turns get folded into compressed_narrative;
    recent_topics + decisions_so_far + pending_clarifications give the
    assembler quick structured handles.
    """

    recent_topics: list[str] = Field(default_factory=list)
    decisions_so_far: list[str] = Field(default_factory=list)
    pending_clarifications: list[str] = Field(default_factory=list)
    compressed_narrative: str | None = None


# Field-presence sets — used by the read_* helpers to tell schemas apart.
_BRD_MEMORY_FIELDS = set(BRDMemory.model_fields.keys())
_SUMMARY_FIELDS = set(ConversationSummary.model_fields.keys())
# A value is a ConversationSummary if it has any unique-to-summary field.
_SUMMARY_DISCRIMINATORS = _SUMMARY_FIELDS - _BRD_MEMORY_FIELDS
# Likewise for BRDMemory.
_BRD_DISCRIMINATORS = _BRD_MEMORY_FIELDS - _SUMMARY_FIELDS


MEMORY_INSTRUCTIONS = """\
You maintain a working memory of a Business Requirements Document (BRD) being \
co-developed with a user.

Extract any BRD-relevant info from the conversation into BRDMemory:
- title and background_notes
- objectives (measurable outcomes)
- stakeholders (name, role, interest)
- functional_requirements (title, description, priority)
- non_functional_requirements (category, description, target)
- constraints, assumptions, risks, open_questions

Rules:
- Prefer to UPDATE the existing memory rather than create duplicates.
- Merge new info into the same fields; deduplicate stakeholders by name.
- Only record what the user has stated or strongly implied. Don't invent.
- Free-form text fields use the user's own phrasing where possible.\
"""

NAMESPACE_PREFIX = ("brd_agent",)
NAMESPACE_TEMPLATE = ("brd_agent", "{langgraph_user_id}")


def _build_langchain_llm() -> AzureChatOpenAI:
    return AzureChatOpenAI(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        temperature=0.0,
    )


def build_memory_manager() -> tuple[BaseStore, object]:
    """Return (store, manager). Manager only handles BRDMemory — the
    rolling summary is produced by the summarize node via a direct LLM
    call (simpler, fewer tokens, easier to reason about per-turn).
    ConversationSummary is still the type-spec for state.rolling_summary.
    """
    store = InMemoryStore()  # no index= → no vector search
    llm = _build_langchain_llm()
    manager = create_memory_store_manager(
        llm,
        schemas=[BRDMemory],
        instructions=MEMORY_INSTRUCTIONS,
        namespace=NAMESPACE_TEMPLATE,
        store=store,
        enable_inserts=True,
        enable_deletes=False,
    )
    return store, manager


def manager_config(session_id: str) -> dict:
    """RunnableConfig used for every manager.invoke call — fills the
    {langgraph_user_id} placeholder in the namespace."""
    return {"configurable": {"langgraph_user_id": session_id}}


def _classify(value: dict) -> str:
    """Return 'brd_memory' | 'summary' | 'unknown' based on field signature."""
    if any(k in value for k in _SUMMARY_DISCRIMINATORS):
        return "summary"
    if any(k in value for k in _BRD_DISCRIMINATORS):
        return "brd_memory"
    return "unknown"


def _flatten_value(item_value) -> Optional[dict]:
    """LangMem wraps stored items as `{"content": <dump>}`. Unwrap that
    layer so the caller sees the plain Pydantic dump."""
    if isinstance(item_value, dict) and "content" in item_value and isinstance(
        item_value["content"], dict
    ):
        return item_value["content"]
    if isinstance(item_value, dict):
        return item_value
    return None


def _merge_dicts(merged: dict, incoming: dict) -> dict:
    """Merge incoming into merged. Lists extend with naive dedupe;
    scalars take latest-wins."""
    for k, v in incoming.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            merged.setdefault(k, [])
            for x in v:
                if x not in merged[k]:
                    merged[k].append(x)
        else:
            merged[k] = v
    return merged


def read_memory(store: BaseStore, session_id: str) -> Optional[dict]:
    """Merge all BRDMemory items in the namespace into a single dict.

    Ignores ConversationSummary items in the same namespace (those are
    read via read_summary).
    """
    namespace = ("brd_agent", session_id)
    items = store.search(namespace)
    if not items:
        return None

    merged: dict = {}
    for item in items:
        value = _flatten_value(item.value)
        if value is None:
            continue
        if _classify(value) != "brd_memory":
            continue
        _merge_dicts(merged, value)
    return merged or None


def read_summary(store: BaseStore, session_id: str) -> Optional[dict]:
    """Merge all ConversationSummary items in the namespace into a single dict.

    Multiple summarize-node invocations may produce multiple items
    (one per trim event). compressed_narrative concatenates with `\\n\\n`;
    lists extend with dedupe.
    """
    namespace = ("brd_agent", session_id)
    items = store.search(namespace)
    if not items:
        return None

    merged: dict = {}
    for item in items:
        value = _flatten_value(item.value)
        if value is None:
            continue
        if _classify(value) != "summary":
            continue
        for k, v in value.items():
            if v is None or v == "" or v == []:
                continue
            if k == "compressed_narrative":
                merged[k] = (
                    (merged.get(k) + "\n\n" + v) if merged.get(k) else v
                )
            elif isinstance(v, list):
                merged.setdefault(k, [])
                for x in v:
                    if x not in merged[k]:
                        merged[k].append(x)
            else:
                merged[k] = v
    return merged or None


def memory_summary(memory: Optional[dict]) -> dict:
    """Compact summary for span attributes."""
    if not memory:
        return {"items_count": 0, "fields_populated": ""}
    populated = [k for k, v in memory.items() if v]
    return {
        "items_count": len(populated),
        "fields_populated": ",".join(populated),
        "has_objectives": bool(memory.get("objectives")),
        "has_stakeholders": bool(memory.get("stakeholders")),
        "has_frs": bool(memory.get("functional_requirements")),
        "has_nfrs": bool(memory.get("non_functional_requirements")),
    }
