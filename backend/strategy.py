"""ContextStrategy enum + infer_strategy().

Strategy decides which slice of state + which retrievers the assembler
pulls into the prompt for THIS turn. Six values, mirroring
ws8_chassis/context/schema.py:

  INFORMATION_GATHERING       open-ended Q&A; minimal LTM, modest STM
  REQUIREMENT_CONSOLIDATION   "let's organise what we have"; full LTM
  BRD_GENERATION              drafting; full memory + template-heavy retrieval
  REVIEW_FEEDBACK             reviewing an existing draft; draft + STM
  REFINEMENT                  iterating on a draft; draft + STM + relevant LTM
  TOOL_REASONING              tool-call decision; minimal context

Inference priority:
  1. Explicit mode override (caller passed mode="drafting" / "request_changes")
  2. Keyword match on the user message
  3. Default: INFORMATION_GATHERING
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class ContextStrategy(str, Enum):
    INFORMATION_GATHERING = "INFORMATION_GATHERING"
    REQUIREMENT_CONSOLIDATION = "REQUIREMENT_CONSOLIDATION"
    BRD_GENERATION = "BRD_GENERATION"
    REVIEW_FEEDBACK = "REVIEW_FEEDBACK"
    REFINEMENT = "REFINEMENT"
    TOOL_REASONING = "TOOL_REASONING"


_BRD_GEN_KEYWORDS = {
    "generate brd", "create brd", "draft brd", "write brd",
    "generate document", "create document", "draft document",
}
_REFINE_KEYWORDS = {"update", "modify", "change section", "revise", "edit"}
_REVIEW_KEYWORDS = {"review", "check", "validate", "look at", "approve"}
_CONSOL_KEYWORDS = {"organise", "organize", "structure", "consolidate", "summarize requirements"}
_TOOL_KEYWORDS = {"find", "search", "look up", "retrieve"}


def infer_strategy(
    *,
    mode: Optional[str] = None,
    user_message: Optional[str] = None,
    has_draft: bool = False,
) -> ContextStrategy:
    """Pick the strategy for this turn.

    Mode-driven overrides take precedence over keyword inference because
    they reflect explicit UI intent (button clicks, endpoint choice):

      mode="drafting"          → BRD_GENERATION   (Generate BRD button)
      mode="request_changes"   → REFINEMENT       (Request Changes button)

    For gathering-mode turns, fall back to keyword inference on the
    user message; default INFORMATION_GATHERING if nothing matches.
    """
    if mode == "drafting":
        return ContextStrategy.BRD_GENERATION
    if mode == "request_changes" and has_draft:
        return ContextStrategy.REFINEMENT

    if not user_message:
        return ContextStrategy.INFORMATION_GATHERING

    m = user_message.lower()
    if any(kw in m for kw in _BRD_GEN_KEYWORDS):
        return ContextStrategy.BRD_GENERATION
    if any(kw in m for kw in _REFINE_KEYWORDS) and has_draft:
        return ContextStrategy.REFINEMENT
    if any(kw in m for kw in _REVIEW_KEYWORDS) and has_draft:
        return ContextStrategy.REVIEW_FEEDBACK
    if any(kw in m for kw in _CONSOL_KEYWORDS):
        return ContextStrategy.REQUIREMENT_CONSOLIDATION
    if any(kw in m for kw in _TOOL_KEYWORDS):
        return ContextStrategy.TOOL_REASONING

    return ContextStrategy.INFORMATION_GATHERING
