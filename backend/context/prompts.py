"""System prompts and message assembly for both agent modes.

Two prompts:
  - GATHERING_SYSTEM_PROMPT: free-form Q&A to elicit BRD info
  - DRAFTING_SYSTEM_PROMPT: produce a strict BRDResponse JSON

build_messages() composes the OpenAI messages list from mode + conversation
+ optional memory + optional draft-for-changes context.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional


GATHERING_SYSTEM_PROMPT = """\
You are a senior business analyst helping a stakeholder articulate a Business \
Requirements Document (BRD).

Your job in this conversation is to gather, through natural dialogue:
- Business objectives (measurable outcomes)
- Stakeholders (name, role, interest)
- Functional requirements (what the system must do)
- Non-functional requirements (performance, security, availability, etc.)
- Constraints, assumptions, dependencies, and risks

Style:
- Ask one focused question at a time. Build on what the user just said.
- Don't dump a checklist of categories — discover them through conversation.
- If the user mentions something concrete (a metric, a name, a deadline), \
acknowledge it briefly so they know you captured it.
- If a CURRENT_MEMORY block is included below, you have already captured those \
items. Don't re-ask. Build on them.
- If the user asks you to "draft" or "generate" the BRD, tell them to click the \
"Generate BRD" button — you can't draft from this conversational mode.

Output: plain conversational text. Do NOT output JSON in this mode.\
"""

DRAFTING_SYSTEM_PROMPT = """\
You are a senior business analyst. Produce a structured Business Requirements \
Document (BRD) as a JSON object based on the conversation and gathered memory.

The BRD JSON must contain these fields:
- title (string)
- background (string): 2-4 sentences of business context
- objectives (array, min 1): measurable business outcomes
- stakeholders (array, min 1): each {name, role, interest}
- functional_requirements (array, min 1): each {id ("FR-001" style), title, \
description, priority, acceptance_criteria (array, min 1)}
- non_functional_requirements (array, may be empty): each {id ("NFR-001" style), \
category, description, target}
- acceptance_criteria (array, min 1): top-level pass conditions for the BRD
- assumptions (array, may be empty)
- out_of_scope (array, may be empty)
- dependencies (array, may be empty)
- risks (array, may be empty): each {description, likelihood, impact, mitigation}

Allowed enum values:
- priority, likelihood, impact: "low" | "medium" | "high" | "critical"
- NFR category: "performance" | "security" | "availability" | "scalability" \
| "usability" | "compliance" | "maintainability"

If the conversation is thin on a section, make reasonable assumptions and list \
them in `assumptions`. Every functional requirement MUST have at least one
acceptance criterion.

Return ONLY the JSON object. No preamble, no markdown fences, no commentary.\
"""

SUMMARIZE_SYSTEM_PROMPT = """\
You compress an older slice of conversation into a structured summary.

Output ONLY a JSON object matching this exact shape:

{
  "recent_topics": ["topic phrase", ...],
  "decisions_so_far": ["decision phrase", ...],
  "pending_clarifications": ["open question phrase", ...],
  "compressed_narrative": "2-4 sentence prose summary"
}

Rules:
- recent_topics: 3-7 high-level topics raised in the slice
- decisions_so_far: explicit commitments or choices the user made
- pending_clarifications: questions the user hasn't answered yet
- compressed_narrative: terse, no preamble, no markdown
- All fields required; use [] or "" when nothing applies.
Return ONLY the JSON object. No preamble, no markdown fences.\
"""

GATHERING_PROMPT_ID = "brd-gathering"
DRAFTING_PROMPT_ID = "brd-drafting"
SUMMARIZE_PROMPT_ID = "brd-summarize"
GATHERING_PROMPT_VERSION = "0.1.0"
DRAFTING_PROMPT_VERSION = "0.1.0"
SUMMARIZE_PROMPT_VERSION = "0.1.0"
GATHERING_PROMPT_HASH = hashlib.sha256(GATHERING_SYSTEM_PROMPT.encode()).hexdigest()
DRAFTING_PROMPT_HASH = hashlib.sha256(DRAFTING_SYSTEM_PROMPT.encode()).hexdigest()
SUMMARIZE_PROMPT_HASH = hashlib.sha256(SUMMARIZE_SYSTEM_PROMPT.encode()).hexdigest()


Mode = Literal["gathering", "drafting"]


def _render_memory_block(memory: dict | None) -> str:
    if not memory:
        return ""
    return (
        "\n\nCURRENT_MEMORY (already captured — don't re-ask):\n"
        + json.dumps(memory, indent=2, default=str)
    )


def _render_draft_for_changes(draft: dict | None, feedback: str | None) -> str:
    if not draft:
        return ""
    block = (
        "\n\nCURRENT_DRAFT (the user just reviewed this BRD):\n"
        + json.dumps(draft, indent=2, default=str)
    )
    if feedback:
        block += f"\n\nUSER_FEEDBACK_ON_DRAFT:\n{feedback}\n\n"
        block += (
            "Acknowledge the feedback in conversation, ask any clarifying questions, "
            "then tell the user to click 'Generate BRD' again when ready."
        )
    return block


def build_messages(
    *,
    mode: Mode,
    conversation: list[dict],
    memory: Optional[dict] = None,
    draft_for_changes: Optional[dict] = None,
    feedback: Optional[str] = None,
) -> list[dict]:
    """Assemble the OpenAI messages list for either mode.

    - `conversation` is the running user/assistant turn history.
    - `memory` (gathering mode) is the latest BRDMemory dict, injected into
      the system prompt so the agent doesn't re-ask captured info.
    - `draft_for_changes` (request-changes path) is a recently rejected BRD
      draft; inject it + feedback into the system context.
    """
    if mode == "gathering":
        system = GATHERING_SYSTEM_PROMPT
        system += _render_memory_block(memory)
        system += _render_draft_for_changes(draft_for_changes, feedback)
        return [{"role": "system", "content": system}, *conversation]

    if mode == "drafting":
        system = DRAFTING_SYSTEM_PROMPT
        system += _render_memory_block(memory)
        return [{"role": "system", "content": system}, *conversation]

    raise ValueError(f"Unknown mode: {mode}")
