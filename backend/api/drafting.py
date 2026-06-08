"""Drafting endpoint.

  POST /generate-brd      { session_id }             → { draft, mode }
  GET  /drafts            { session_id }             → { versions, count, latest_status }
  GET  /drafts/{version}  { session_id }             → { entry, draft }
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from langgraph.errors import GraphInterrupt
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.api.deps import _config, _current_draft, _read_graph_state
from backend.core.schema import BRDResponse, DraftDetailResponse, DraftsListResponse
from backend.telemetry import (
    KIND_AGENT,
    chat_span,
    set_input,
    set_outcome,
    set_output,
)

router = APIRouter()


class GenerateBRDRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class DraftResponse(BaseModel):
    draft: BRDResponse
    mode: str


@router.post("/generate-brd", response_model=DraftResponse)
def generate_brd(body: GenerateBRDRequest, request: Request) -> DraftResponse:
    graph = request.app.state.drafting_graph
    with chat_span(
        "brd_agent.draft",
        session_id=body.session_id,
        span_kind=KIND_AGENT,
        otel_kind=SpanKind.SERVER,
        chat_mode="drafting",
        tags=["drafting"],
        track_outcome=True,
    ) as root:
        try:
            result = graph.invoke(
                {"mode": "drafting"},
                config=_config(body.session_id),
            )
        except GraphInterrupt:
            # HITL 2: graph paused after schema_validate (interrupt_after)
            # The draft is already persisted in state by schema_validate
            state = _read_graph_state(request, body.session_id)
            draft_dict = state.get("current_draft")
            if draft_dict is None:
                raise RuntimeError(
                    "schema_validate completed but no current_draft in state"
                )
            draft = BRDResponse.model_validate(draft_dict)
            root.set_attribute("brd.id", str(draft.brd_id))
            set_input(root, f"draft from current thread state")
            set_output(
                root,
                {
                    "brd_id": str(draft.brd_id),
                    "title": draft.title,
                    "frs": len(draft.functional_requirements),
                    "nfrs": len(draft.non_functional_requirements),
                    "stakeholders": [s.name for s in draft.stakeholders],
                },
                mime="application/json",
            )
            set_outcome(root, "success")
            return DraftResponse(draft=draft, mode="awaiting_approval")

        # Normal completion (without interrupt_after, shouldn't happen)
        draft_dict = result.get("current_draft")
        if draft_dict is None:
            raise RuntimeError("drafting_graph did not produce a current_draft")
        draft = BRDResponse.model_validate(draft_dict)
        root.set_attribute("brd.id", str(draft.brd_id))
        set_input(root, f"draft from current thread state")
        set_output(
            root,
            {
                "brd_id": str(draft.brd_id),
                "title": draft.title,
                "frs": len(draft.functional_requirements),
                "nfrs": len(draft.non_functional_requirements),
                "stakeholders": [s.name for s in draft.stakeholders],
            },
            mime="application/json",
        )
        set_outcome(root, "success")

    return DraftResponse(draft=draft, mode="awaiting_approval")


@router.get("/drafts", response_model=DraftsListResponse)
def list_drafts(session_id: str, request: Request) -> DraftsListResponse:
    state = _read_graph_state(request, session_id)
    history = state.get("draft_history") or []
    versions = [e["version"] for e in history]
    latest_status = history[-1]["status"] if history else None
    return DraftsListResponse(
        session_id=session_id,
        versions=versions,
        count=len(history),
        latest_status=latest_status,
    )


@router.get("/drafts/{version}", response_model=DraftDetailResponse)
def get_draft(version: int, session_id: str, request: Request) -> DraftDetailResponse:
    state = _read_graph_state(request, session_id)
    history = state.get("draft_history") or []
    entry = next((e for e in history if e["version"] == version), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Draft version {version} not found."
        )
    draft = BRDResponse.model_validate(entry["draft"])
    return DraftDetailResponse(session_id=session_id, entry=entry, draft=draft)
