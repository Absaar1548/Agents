"""Review endpoints.

  POST /approve           {}             → { status, brd_id }
  POST /request-changes   { feedback }   → { reply, draft, mode }
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import HumanMessage
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.api.deps import _config, _current_draft
from backend.core.schema import BRDResponse
from backend.session import get_session
from backend.telemetry import (
    KIND_AGENT,
    KIND_CHAIN,
    chat_span,
    set_input,
    set_outcome,
    set_output,
)

router = APIRouter()


class ApproveResponse(BaseModel):
    status: str
    brd_id: str


class RequestChangesBody(BaseModel):
    feedback: str = Field(min_length=1)


class RequestChangesResponse(BaseModel):
    reply: str
    draft: BRDResponse
    mode: str


@router.post("/approve", response_model=ApproveResponse)
def approve(request: Request) -> ApproveResponse:
    session = get_session()
    draft = _current_draft(request, session.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to approve.")
    brd_id = str(draft.brd_id)
    with chat_span(
        "brd_agent.approve",
        session_id=session.session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.SERVER,
        chat_mode="approve",
        tags=["approve"],
        track_outcome=True,
    ) as span:
        set_input(span, {"brd_id": brd_id}, mime="application/json")
        span.set_attribute("brd.id", brd_id)
        span.set_attribute("approval.outcome", "approved")
        session.approved = True
        set_output(
            span, {"status": "approved", "brd_id": brd_id}, mime="application/json"
        )
        set_outcome(span, "approved")
    return ApproveResponse(status="approved", brd_id=brd_id)


@router.post("/request-changes", response_model=RequestChangesResponse)
def request_changes(body: RequestChangesBody, request: Request) -> RequestChangesResponse:
    session = get_session()
    draft = _current_draft(request, session.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to revise.")

    graph = request.app.state.gathering_graph

    with chat_span(
        "brd_agent.request_changes",
        session_id=session.session_id,
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
            config=_config(session.session_id),
        )
        reply = result.get("reply_text", "")

        set_output(root, reply)
        set_outcome(root, "success")

    return RequestChangesResponse(reply=reply, draft=draft, mode="gathering")
