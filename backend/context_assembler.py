"""ContextAssembler — single seam for per-turn prompt assembly.

Inputs (from LangGraph state):
  messages_in_state  list[BaseMessage]  raw transcript (LangGraph add_messages)
  brd_memory         Optional[dict]     typed facts (BRDMemory dump)
  rolling_summary    Optional[dict]     compressed older turns (ConversationSummary dump)
  current_draft      Optional[dict]     BRDResponse JSON (set after generate-brd)
  feedback           Optional[str]      request-changes feedback (last HumanMessage)

Output: AssembledContext with:
  - strategy            ContextStrategy used
  - messages            list[dict] in OpenAI chat format, stable → volatile
                        order for Azure prefix caching
  - token_accounting    per-section token counts (drives span attrs)
  - dropped_sections    sections cut by budget enforcement
  - provenance          source IDs (sessions, prompt IDs, future: doc/kg IDs)
  - prompt_id / version / template_hash  for the LLM-call span

Phase 2 owns layers 1a (transcript), 1b (summary), and 2 (typed facts).
Phase 3 will add doc-retrieval calls before token accounting; Phase 4 will
add KG calls in the same place.

Strategy-driven knobs (per WS#5 trace contract §6 / strategy.py):
  TOOL_REASONING         minimal context; last 2 turns, no summary,
                         memory: title + objectives only
  INFORMATION_GATHERING  last 10 turns, summary if present,
                         memory: title + objectives + open_questions
  REQUIREMENT_CONSOLIDATION, REVIEW_FEEDBACK, REFINEMENT
                         last 10 turns, summary, full memory
  BRD_GENERATION         all turns, summary, full memory

  REVIEW_FEEDBACK / REFINEMENT additionally inject the current_draft +
  feedback into a system block so the LLM can iterate against the draft.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.prompts import (
    DRAFTING_PROMPT_HASH,
    DRAFTING_PROMPT_ID,
    DRAFTING_PROMPT_VERSION,
    DRAFTING_SYSTEM_PROMPT,
    GATHERING_PROMPT_HASH,
    GATHERING_PROMPT_ID,
    GATHERING_PROMPT_VERSION,
    GATHERING_SYSTEM_PROMPT,
)
from backend.sources.base import RetrievalSource, RetrievedDoc
from backend.strategy import ContextStrategy
from backend.telemetry import source_span


# ----- tokenizer (gpt-4o family) -----
_ENC = tiktoken.encoding_for_model("gpt-4o")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_ENC.encode(text))


# ----- output dataclasses -----
class TokenAccounting(BaseModel):
    system_prompt: int = 0
    memory: int = 0
    summary: int = 0
    draft_block: int = 0
    docs: int = 0
    kg: int = 0
    artifacts: int = 0
    conversation: int = 0
    total: int = 0


class AssembledContext(BaseModel):
    strategy: ContextStrategy
    messages: list[dict] = Field(default_factory=list)
    token_accounting: TokenAccounting = Field(default_factory=TokenAccounting)
    dropped_sections: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_template_hash: str = ""


# ----- per-strategy policies -----
# How many trailing raw turns to include. "all" means no slicing.
_TURNS_BY_STRATEGY: dict[ContextStrategy, int | str] = {
    ContextStrategy.TOOL_REASONING: 2,
    ContextStrategy.INFORMATION_GATHERING: 10,
    ContextStrategy.REQUIREMENT_CONSOLIDATION: 10,
    ContextStrategy.REVIEW_FEEDBACK: 10,
    ContextStrategy.REFINEMENT: 10,
    ContextStrategy.BRD_GENERATION: "all",
}

_INCLUDE_SUMMARY = {
    ContextStrategy.INFORMATION_GATHERING,
    ContextStrategy.REQUIREMENT_CONSOLIDATION,
    ContextStrategy.REVIEW_FEEDBACK,
    ContextStrategy.REFINEMENT,
    ContextStrategy.BRD_GENERATION,
}

_INJECT_DRAFT = {
    ContextStrategy.REVIEW_FEEDBACK,
    ContextStrategy.REFINEMENT,
}


# Per-strategy doc retrieval: (k, source_focus). source_focus is advisory
# (not enforced) — kept for future when we add metadata-filtered queries.
# TOOL_REASONING absent → no doc retrieval for that strategy.
_DOCS_POLICY: dict[ContextStrategy, tuple[int, str]] = {
    ContextStrategy.INFORMATION_GATHERING: (3, "glossary"),
    ContextStrategy.REQUIREMENT_CONSOLIDATION: (3, "mixed"),
    ContextStrategy.BRD_GENERATION: (4, "template+priors"),
    ContextStrategy.REVIEW_FEEDBACK: (2, "mixed"),
    ContextStrategy.REFINEMENT: (2, "mixed"),
}

# Per-strategy KG retrieval: number of seed entities to fetch (each
# entity carries 1-hop neighbours in its rendered string). TOOL_REASONING
# skipped to keep that path minimal.
_KG_POLICY: dict[ContextStrategy, int] = {
    ContextStrategy.INFORMATION_GATHERING: 3,
    ContextStrategy.REQUIREMENT_CONSOLIDATION: 3,
    ContextStrategy.BRD_GENERATION: 4,
    ContextStrategy.REVIEW_FEEDBACK: 2,
    ContextStrategy.REFINEMENT: 2,
}


# ----- helpers -----
def _to_openai_msgs(messages: list[BaseMessage]) -> list[dict]:
    """Convert LangGraph BaseMessage list → OpenAI chat dicts."""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": m.content})
        # SystemMessage / ToolMessage handled in Phase 5
    return out


def _slice_turns(
    messages: list[dict], strategy: ContextStrategy
) -> tuple[list[dict], int]:
    """Apply per-strategy slicing; return (sliced, dropped_count)."""
    policy = _TURNS_BY_STRATEGY[strategy]
    if policy == "all":
        return messages, 0
    n = int(policy)
    if len(messages) <= n:
        return messages, 0
    return messages[-n:], len(messages) - n


def _filter_memory(memory: dict, strategy: ContextStrategy) -> dict:
    """Apply per-strategy field filtering to the BRDMemory dump."""
    if strategy == ContextStrategy.TOOL_REASONING:
        keep = {"title", "objectives"}
    elif strategy == ContextStrategy.INFORMATION_GATHERING:
        keep = {"title", "background_notes", "objectives", "open_questions"}
    else:
        return memory  # full memory
    return {k: v for k, v in memory.items() if k in keep and v}


def _render_memory_block(memory: Optional[dict]) -> str:
    if not memory:
        return ""
    return (
        "CURRENT_MEMORY (already captured — don't re-ask):\n"
        + json.dumps(memory, indent=2, default=str)
    )


def _render_summary_block(summary: Optional[dict]) -> str:
    if not summary:
        return ""
    parts = ["CONVERSATION_SUMMARY (older turns, compressed):"]
    if summary.get("compressed_narrative"):
        parts.append(summary["compressed_narrative"])
    for label, key in [
        ("Recent topics", "recent_topics"),
        ("Decisions so far", "decisions_so_far"),
        ("Pending clarifications", "pending_clarifications"),
    ]:
        items = summary.get(key) or []
        if items:
            parts.append(f"{label}:")
            parts.extend(f"  - {x}" for x in items)
    return "\n".join(parts)


def _render_draft_block(
    draft: Optional[dict], feedback: Optional[str]
) -> str:
    if not draft:
        return ""
    block = (
        "CURRENT_DRAFT (the user just reviewed this BRD):\n"
        + json.dumps(draft, indent=2, default=str)
    )
    if feedback:
        block += f"\n\nUSER_FEEDBACK_ON_DRAFT:\n{feedback}"
    return block


def _docs_query(
    strategy: ContextStrategy,
    user_message: Optional[str],
    brd_memory: Optional[dict],
    feedback: Optional[str],
) -> str:
    """Pick the query text to feed Chroma based on strategy."""
    if strategy == ContextStrategy.BRD_GENERATION:
        # Memory-driven: use title + objectives as the query.
        parts: list[str] = []
        if brd_memory:
            if brd_memory.get("title"):
                parts.append(str(brd_memory["title"]))
            objs = brd_memory.get("objectives") or []
            parts.extend(str(o) for o in objs[:3])
        return " ".join(parts) if parts else (user_message or "")
    if strategy in (ContextStrategy.REVIEW_FEEDBACK, ContextStrategy.REFINEMENT):
        return feedback or user_message or ""
    return user_message or ""


def _render_docs_block(hits: list[RetrievedDoc]) -> str:
    if not hits:
        return ""
    lines = ["RELEVANT_REFERENCES (from internal corpus, ordered by similarity):"]
    for h in hits:
        kind = h.metadata.get("kind", "doc")
        label = h.metadata.get("term") or h.metadata.get("title") or h.id
        lines.append(f"- [{kind}/{label}] {h.text}")
    return "\n".join(lines)


def _render_kg_block(hits: list[RetrievedDoc]) -> str:
    if not hits:
        return ""
    lines = ["KNOWLEDGE_GRAPH (related entities and 1-hop relations):"]
    for h in hits:
        lines.append(f"- {h.text}")
    return "\n".join(lines)


def _render_artifact_block(artifact_refs: list[dict]) -> str:
    """Render artifact summaries pulled from state.last_retrievals.

    Each ref is an ArtifactRef.model_dump() dict (artifact_id, artifact_type,
    name, summary, size_bytes, created_at). We only inject the summary into
    the prompt; the full content stays in the ArtifactStore and is only
    fetched on demand by tools that need it.
    """
    if not artifact_refs:
        return ""
    lines = ["TOOL_ARTIFACTS (summaries of previously fetched large outputs):"]
    for r in artifact_refs:
        atype = r.get("artifact_type", "artifact")
        name = r.get("name", "?")
        aid = r.get("artifact_id", "?")
        size = r.get("size_bytes", 0)
        summary = r.get("summary", "")
        lines.append(
            f"- [{atype}/{name} · id={aid[:8]} · {size}B] {summary}"
        )
    return "\n".join(lines)


# ----- main entry point -----
class ContextAssembler:
    """Wires together state + external retrieval sources into one prompt.

    Stateless w.r.t. per-turn data; holds source handles (docs_source,
    kg_source) at construction. Build once at process startup.

    Sources are optional — when None, the assembler simply skips that
    layer's retrieval (useful for tests and phased rollout).
    """

    def __init__(
        self,
        docs_source: Optional[RetrievalSource] = None,
        kg_source: Optional[RetrievalSource] = None,
    ):
        self.docs_source = docs_source
        self.kg_source = kg_source  # Phase 4 wires this

    def _retrieve_docs(
        self,
        *,
        strategy: ContextStrategy,
        session_id: str,
        user_message: Optional[str],
        brd_memory: Optional[dict],
        feedback: Optional[str],
    ) -> list[RetrievedDoc]:
        if self.docs_source is None or strategy not in _DOCS_POLICY:
            return []
        k, focus = _DOCS_POLICY[strategy]
        query = _docs_query(strategy, user_message, brd_memory, feedback)
        with source_span(
            "context.retrieve.docs",
            session_id=session_id,
            source_kind=self.docs_source.name,
            query=query,
            k=k,
            focus=focus,
        ) as span:
            hits = self.docs_source.retrieve(query=query, k=k) if query else []
            span.set_attribute("source.hits_count", len(hits))
            if hits:
                span.set_attribute(
                    "source.hit_ids", ",".join(h.id for h in hits)
                )
                span.set_attribute(
                    "source.distance_min",
                    min(
                        (h.distance for h in hits if h.distance is not None),
                        default=0.0,
                    ),
                )
            return hits

    def _retrieve_kg(
        self,
        *,
        strategy: ContextStrategy,
        session_id: str,
        user_message: Optional[str],
        brd_memory: Optional[dict],
        feedback: Optional[str],
    ) -> list[RetrievedDoc]:
        if self.kg_source is None or strategy not in _KG_POLICY:
            return []
        k = _KG_POLICY[strategy]
        # Same query construction as docs — entity matching benefits from
        # full context (memory titles / objectives during drafting,
        # feedback during refinement, user message everywhere else).
        query = _docs_query(strategy, user_message, brd_memory, feedback)
        with source_span(
            "context.retrieve.kg",
            session_id=session_id,
            source_kind=self.kg_source.name,
            query=query,
            k=k,
        ) as span:
            hits = self.kg_source.retrieve(query=query, k=k) if query else []
            span.set_attribute("source.hits_count", len(hits))
            if hits:
                span.set_attribute(
                    "source.hit_ids", ",".join(h.id for h in hits)
                )
                # KG-specific attrs surfaced alongside the uniform source.*
                span.set_attribute(
                    "kg.entity_labels",
                    ",".join(
                        h.metadata.get("entity_label", "?") for h in hits
                    ),
                )
                span.set_attribute(
                    "kg.neighbours_total",
                    sum(h.metadata.get("neighbours", 0) for h in hits),
                )
            return hits

    def assemble(
        self,
        *,
        strategy: ContextStrategy,
        session_id: str,
        messages_in_state: list[BaseMessage],
        brd_memory: Optional[dict] = None,
        rolling_summary: Optional[dict] = None,
        current_draft: Optional[dict] = None,
        feedback: Optional[str] = None,
        artifact_refs: Optional[list[dict]] = None,
    ) -> AssembledContext:
        # Pick prompt by strategy: BRD_GENERATION → drafting, else gathering.
        if strategy == ContextStrategy.BRD_GENERATION:
            system_prompt_text = DRAFTING_SYSTEM_PROMPT
            prompt_id = DRAFTING_PROMPT_ID
            prompt_version = DRAFTING_PROMPT_VERSION
            prompt_hash = DRAFTING_PROMPT_HASH
        else:
            system_prompt_text = GATHERING_SYSTEM_PROMPT
            prompt_id = GATHERING_PROMPT_ID
            prompt_version = GATHERING_PROMPT_VERSION
            prompt_hash = GATHERING_PROMPT_HASH

        # Per-strategy memory filter
        filtered_memory = (
            _filter_memory(brd_memory, strategy) if brd_memory else None
        )
        memory_block = _render_memory_block(filtered_memory)

        # Summary
        include_summary = strategy in _INCLUDE_SUMMARY
        summary_block = _render_summary_block(rolling_summary) if include_summary else ""

        # Draft injection for review/refinement
        draft_block = ""
        if strategy in _INJECT_DRAFT:
            draft_block = _render_draft_block(current_draft, feedback)

        # Determine the user message used for doc query / strategy inference
        last_user_msg: Optional[str] = None
        for m in reversed(messages_in_state):
            if isinstance(m, HumanMessage):
                last_user_msg = m.content
                break

        # Layer 3: doc retrieval (Vector DB)
        docs_hits = self._retrieve_docs(
            strategy=strategy,
            session_id=session_id,
            user_message=last_user_msg,
            brd_memory=brd_memory,
            feedback=feedback,
        )
        docs_block = _render_docs_block(docs_hits)

        # Layer 4: KG retrieval (Neo4j)
        kg_hits = self._retrieve_kg(
            strategy=strategy,
            session_id=session_id,
            user_message=last_user_msg,
            brd_memory=brd_memory,
            feedback=feedback,
        )
        kg_block = _render_kg_block(kg_hits)

        # Layer 5 (cross-cutting): artifact summaries previously stashed by
        # tool calls. The full payloads live in ArtifactStore; we only
        # surface summaries into the prompt.
        artifact_block = _render_artifact_block(artifact_refs or [])

        # Compose the system message: stable → volatile prefix order
        system_parts = [system_prompt_text]
        if memory_block:
            system_parts.append(memory_block)
        if summary_block:
            system_parts.append(summary_block)
        if docs_block:
            system_parts.append(docs_block)
        if kg_block:
            system_parts.append(kg_block)
        if artifact_block:
            system_parts.append(artifact_block)
        if draft_block:
            system_parts.append(draft_block)
        system_content = "\n\n".join(system_parts)

        # Conversation slice
        all_turns = _to_openai_msgs(messages_in_state)
        sliced_turns, dropped_turn_count = _slice_turns(all_turns, strategy)

        messages: list[dict] = [{"role": "system", "content": system_content}, *sliced_turns]

        # Token accounting
        acct = TokenAccounting(
            system_prompt=count_tokens(system_prompt_text),
            memory=count_tokens(memory_block),
            summary=count_tokens(summary_block),
            draft_block=count_tokens(draft_block),
            docs=count_tokens(docs_block),
            kg=count_tokens(kg_block),
            artifacts=count_tokens(artifact_block),
            conversation=sum(count_tokens(m["content"]) for m in sliced_turns),
        )
        acct.total = (
            acct.system_prompt
            + acct.memory
            + acct.summary
            + acct.draft_block
            + acct.docs
            + acct.kg
            + acct.artifacts
            + acct.conversation
        )

        dropped: list[str] = []
        if dropped_turn_count > 0:
            dropped.append(f"stm_oldest_turns={dropped_turn_count}")

        return AssembledContext(
            strategy=strategy,
            messages=messages,
            token_accounting=acct,
            dropped_sections=dropped,
            provenance={
                "session_id": session_id,
                "prompt_id": prompt_id,
                "memory_present": brd_memory is not None,
                "summary_present": rolling_summary is not None,
                "draft_present": current_draft is not None,
                "docs_hit_ids": [h.id for h in docs_hits],
                "docs_source": self.docs_source.name if self.docs_source else None,
                "kg_hit_ids": [h.id for h in kg_hits],
                "kg_source": self.kg_source.name if self.kg_source else None,
                "artifact_ids": [
                    r.get("artifact_id") for r in (artifact_refs or [])
                ],
            },
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_template_hash=prompt_hash,
        )
