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
  2. Semantic router (embedding-based classification via semantic-router + fastembed)
  3. Default: INFORMATION_GATHERING
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from semantic_router import Route, SemanticRouter
from semantic_router.encoders import FastEmbedEncoder

logger = logging.getLogger(__name__)


class ContextStrategy(str, Enum):
    INFORMATION_GATHERING = "INFORMATION_GATHERING"
    REQUIREMENT_CONSOLIDATION = "REQUIREMENT_CONSOLIDATION"
    BRD_GENERATION = "BRD_GENERATION"
    REVIEW_FEEDBACK = "REVIEW_FEEDBACK"
    REFINEMENT = "REFINEMENT"
    TOOL_REASONING = "TOOL_REASONING"


# ----- semantic-router routes -----
# Each route targets one of the keyword-inferrable strategies.
# Utterances are varied phrasings a user might type to trigger that strategy.
# score_threshold=0.45 is the floor — lower scores fall through to default.

_bg_route = Route(
    name="BRD_GENERATION",
    utterances=[
        "generate the BRD",
        "draft the BRD",
        "create the BRD document",
        "write the BRD",
        "make a BRD now",
        "produce the requirements document",
        "generate the requirements",
        "draft the document",
    ],
    score_threshold=0.45,
)

_refine_route = Route(
    name="REFINEMENT",
    utterances=[
        "change the scope section",
        "modify FR-001",
        "revise the assumptions",
        "update the stakeholder list",
        "edit the risks",
        "update the non-functional requirements",
        "rewrite the background",
        "fix the dependencies",
    ],
    score_threshold=0.45,
)

_review_route = Route(
    name="REVIEW_FEEDBACK",
    utterances=[
        "review the draft",
        "check the BRD",
        "validate the requirements",
        "look at the document",
        "examine the requirements",
        "can you check the BRD",
    ],
    score_threshold=0.45,
)

_consolidate_route = Route(
    name="REQUIREMENT_CONSOLIDATION",
    utterances=[
        "organise what we have",
        "organize what we have",
        "summarize the requirements",
        "consolidate what we discussed",
        "structure the information",
        "summarise what we have gathered",
        "pull together the requirements",
    ],
    score_threshold=0.45,
)

_tool_route = Route(
    name="TOOL_REASONING",
    utterances=[
        "find the template",
        "search for the glossary",
        "look up previous BRDs",
        "retrieve the document",
        "fetch the template",
    ],
    score_threshold=0.45,
)

_ROUTES = [_bg_route, _refine_route, _review_route, _consolidate_route, _tool_route]

# Lazy singleton — initialized once on first inference, not at import time.
_router: Optional[SemanticRouter] = None


def _get_router() -> Optional[SemanticRouter]:
    """Return the cached SemanticRouter singleton, creating it on first call.

    The FastEmbed encoder uses the ONNX BGE-small-en-v1.5 model (~100 MB),
    downloaded once on first use into the fastembed cache.

    If initialization fails the sentinel stays None and inference
    falls back to INFORMATION_GATHERING (default).
    """
    global _router
    if _router is not None:
        return _router
    try:
        encoder = FastEmbedEncoder()
        _router = SemanticRouter(encoder=encoder, routes=_ROUTES, auto_sync="local")
        logger.info("Semantic router initialised with %d routes", len(_ROUTES))
    except Exception:
        logger.warning(
            "Semantic router failed to initialise; "
            "falling back to default strategy for all turns",
            exc_info=True,
        )
        _router = None
    return _router


def infer_strategy(
    *,
    mode: Optional[str] = None,
    user_message: Optional[str] = None,
    has_draft: bool = False,
) -> ContextStrategy:
    """Pick the strategy for this turn.

    Inference priority:
      1. Mode override (explicit UI intent)
         mode="drafting"        → BRD_GENERATION
         mode="request_changes" → REFINEMENT (if a draft exists)
      2. Semantic router (embedding-based classification)
      3. Default: INFORMATION_GATHERING
    """
    # --- 1. Mode-driven overrides ---
    if mode == "drafting":
        return ContextStrategy.BRD_GENERATION
    if mode == "request_changes" and has_draft:
        return ContextStrategy.REFINEMENT

    if not user_message or not user_message.strip():
        return ContextStrategy.INFORMATION_GATHERING

    # --- 2. Semantic router ---
    router = _get_router()
    if router is not None:
        try:
            result = router(user_message)
            if result.name and result.similarity_score is not None:
                strategy = result.name
                if strategy == "BRD_GENERATION":
                    return ContextStrategy.BRD_GENERATION
                if strategy == "REFINEMENT" and has_draft:
                    return ContextStrategy.REFINEMENT
                if strategy == "REVIEW_FEEDBACK" and has_draft:
                    return ContextStrategy.REVIEW_FEEDBACK
                if strategy == "REQUIREMENT_CONSOLIDATION":
                    return ContextStrategy.REQUIREMENT_CONSOLIDATION
                if strategy == "TOOL_REASONING":
                    return ContextStrategy.TOOL_REASONING
        except Exception:
            logger.debug(
                "Semantic router call failed; falling through to default",
                exc_info=True,
            )

    # --- 3. Default ---
    return ContextStrategy.INFORMATION_GATHERING
