"""Drafting endpoint.

  POST /generate-brd      { session_id }             → { draft, mode }
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.api.deps import _config, _current_draft
from backend.core.schema import BRDResponse
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
        result = graph.invoke(
            {"mode": "drafting"},
            config=_config(body.session_id),
        )
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
