"""In-process session shim — Phase 1 simplified.

Now that LangGraph's MemorySaver is the source of truth for state
(messages, mode, current_draft, brd_memory, rolling_summary), this module
only tracks:

  - session_id — used verbatim as the LangGraph thread_id
  - approved   — set by /approve so /chat knows to stop treating new
                 messages as change requests against the approved draft

Single session per process for the PoC. reset_session() rotates the
session_id so the next graph invocation starts with empty thread state.
"""
from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel, Field


def _new_session_id() -> str:
    return uuid.uuid4().hex


class SessionState(BaseModel):
    session_id: str = Field(default_factory=_new_session_id)
    approved: bool = False


_session: Optional[SessionState] = None


def get_session() -> SessionState:
    global _session
    if _session is None:
        _session = SessionState()
    return _session


def reset_session() -> SessionState:
    global _session
    _session = SessionState()
    return _session
