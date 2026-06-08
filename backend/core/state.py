"""LangGraph state definitions for the BRD agent.

ChatbotState is the per-thread state persisted by the checkpointer.
All fields are optional (total=False) so nodes can return partial updates.

Stub fields (ready_for_production, retry_count, etc.) are added here but
not yet wired — they will be activated in Phases 2 and 4.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class DraftStatus(str, Enum):
    """Lifecycle state of a produced BRD draft."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


ChatMode = Literal["gathering", "drafting", "request_changes"]


class ChatbotState(TypedDict, total=False):
    """Per-thread state persisted by the checkpointer.

    `messages` uses LangGraph's `add_messages` reducer so HumanMessage /
    AIMessage / RemoveMessage updates compose correctly across nodes.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    mode: ChatMode
    brd_memory: Optional[dict]       # latest BRDMemory dict
    current_draft: Optional[dict]   # BRDResponse JSON dump (gets set by drafting)
    reply_text: Optional[str]        # plain-text assistant reply (gathering / request_changes)
    rolling_summary: Optional[dict]  # ConversationSummary dump (populated by summarize node)
    last_retrievals: Optional[dict]  # per-turn assembler output + doc/kg/artifact caches

    # --- Phase 2 fields (now wired) ---
    ready_for_production: bool       # Set by invoke_llm → drives conditional edge
    retry_count: int                # Schema validation retries (max 2)
    validation_errors: list[str]    # Pydantic errors fed back to LLM
    draft_status: DraftStatus        # DRAFT | APPROVED | REJECTED
    rejection_count: int            # Tracked for observability (no hard cap)
    draft_history: list[dict]       # Append-only, full provenance per version
    feedback_gathering: bool         # True when user chose "Add more" at HITL 1
    pending_feedback: list[str]      # Accumulated feedback items for next production cycle
