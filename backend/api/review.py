"""Review endpoints.

  POST /approve           { session_id }             → { status, brd_id }
  POST /request-changes   { feedback, session_id }   → { reply, draft, mode }
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import HumanMessage
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.api.deps import _config, _current_draft, _read_graph_state
from backend.core.schema import BRDResponse
from backend.core.state import DraftStatus
from backend.telemetry import (
    KIND_AGENT,
    KIND_CHAIN,
    chat_span,
    set_input,
    set_outcome,
    set_output,
)

router = APIRouter()


class ApproveRequest(BaseModel):
    session_id: str = Field(...)


class ApproveResponse(BaseModel):
    status: str
    brd_id: str


class RequestChangesBody(BaseModel):
    feedback: str = Field(min_length=1)
    session_id: str = Field(...)


class RequestChangesResponse(BaseModel):
    reply: str
    draft: BRDResponse
    mode: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mutate_latest_draft_history(
    graph, config: dict, state: dict, *, status: str, reviewed_by: str
) -> None:
    """Copy draft_history, mutate the latest entry, and write it back."""
    history = list(state.get("draft_history") or [])
    if history:
        history[-1]["status"] = status
        history[-1]["reviewed_by"] = reviewed_by
        history[-1]["reviewed_at"] = _now_iso()
        graph.update_state(config, {"draft_history": history})


@router.post("/approve", response_model=ApproveResponse)
def approve(body: ApproveRequest, request: Request) -> ApproveResponse:
    draft = _current_draft(request, body.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to approve.")
    brd_id = str(draft.brd_id)

    # Persist approval into graph state
    graph = request.app.state.gathering_graph
    config = _config(body.session_id)
    state = _read_graph_state(request, body.session_id)
    graph.update_state(config, {"draft_status": DraftStatus.APPROVED})
    _mutate_latest_draft_history(
        graph, config, state, status="approved", reviewed_by="user"
    )

    with chat_span(
        "brd_agent.approve",
        session_id=body.session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.SERVER,
        chat_mode="approve",
        tags=["approve"],
        track_outcome=True,
    ) as span:
        set_input(span, {"brd_id": brd_id}, mime="application/json")
        span.set_attribute("brd.id", brd_id)
        span.set_attribute("approval.outcome", "approved")
        set_output(
            span, {"status": "approved", "brd_id": brd_id}, mime="application/json"
        )
        set_outcome(span, "approved")
    return ApproveResponse(status="approved", brd_id=brd_id)


@router.post("/request-changes", response_model=RequestChangesResponse)
def request_changes(body: RequestChangesBody, request: Request) -> RequestChangesResponse:
    draft = _current_draft(request, body.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to revise.")

    graph = request.app.state.gathering_graph

    # Mark as rejected + increment rejection_count
    config = _config(body.session_id)
    state = _read_graph_state(request, body.session_id)
    rejection_count = state.get("rejection_count", 0) + 1
    graph.update_state(config, {
        "draft_status": DraftStatus.REJECTED,
        "rejection_count": rejection_count,
    })
    _mutate_latest_draft_history(
        graph, config, state, status="rejected", reviewed_by="user"
    )

    with chat_span(
        "brd_agent.request_changes",
        session_id=body.session_id,
        span_kind=KIND_AGENT,
        otel_kind=SpanKind.SERVER,
        chat_mode="request_changes",
        tags=["request_changes"],
        track_outcome=True,
    ) as root:
        set_input(root, body.feedback)
        root.set_attribute("draft.id", str(draft.brd_id))
        root.set_attribute("user.feedback_length", len(body.feedback))

        result = graph.invoke(
            {
                "messages": [HumanMessage(content=body.feedback)],
                "mode": "request_changes",
            },
            config=_config(body.session_id),
        )
        reply = result.get("reply_text", "")

        set_output(root, reply)
        set_outcome(root, "success")

    return RequestChangesResponse(reply=reply, draft=draft, mode="gathering")
