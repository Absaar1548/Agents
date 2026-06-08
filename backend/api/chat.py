"""Chat, reset, memory inspection, and tool endpoints.

  POST /chat              { message }    → { reply, mode, draft?, turn_id }
  POST /reset             {}             → { status, session_id }
  GET  /memory                           → { session_id, memory }
  POST /tools/fetch-brd-template { template_name } → { artifact_ref, available_templates, note }
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from backend.api.deps import _config, _current_draft, _read_graph_state, _turn_id
from backend.artifacts import ArtifactRef
from backend.core.schema import BRDResponse
from backend.memory import read_memory
from backend.session import get_session, reset_session
from backend.telemetry import (
    KIND_AGENT,
    KIND_CHAIN,
    KIND_TOOL,
    chat_span,
    set_input,
    set_outcome,
    set_output,
)
from backend.tools.fetch_brd_template import available_templates, fetch_brd_template

router = APIRouter()


# ----- request / response models -----
class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    mode: str
    draft: Optional[BRDResponse] = None
    turn_id: int


class ResetResponse(BaseModel):
    status: str
    session_id: str


class MemoryResponse(BaseModel):
    session_id: str
    memory: Optional[dict] = None


class FetchTemplateBody(BaseModel):
    template_name: Optional[str] = None


class FetchTemplateResponse(BaseModel):
    artifact_ref: ArtifactRef
    available_templates: list[str]
    note: str


# ----- endpoints -----
@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
    session = get_session()
    existing_draft = _current_draft(request, session.session_id)
    graph = request.app.state.gathering_graph

    # If a draft is awaiting approval and the user sends plain chat, treat
    # the message as a change request so the draft is injected into the prompt.
    if existing_draft is not None and not session.approved:
        mode = "request_changes"
        root_name = "brd_agent.request_changes"
        tags = ["request_changes"]
    else:
        mode = "gathering"
        root_name = "brd_agent.turn"
        tags = ["gathering"]

    with chat_span(
        root_name,
        session_id=session.session_id,
        span_kind=KIND_AGENT,
        otel_kind=SpanKind.SERVER,
        chat_mode=mode,
        tags=tags,
        track_outcome=True,
    ) as root:
        set_input(root, body.message)
        if existing_draft:
            root.set_attribute("draft.id", str(existing_draft.brd_id))

        result = graph.invoke(
            {"messages": [HumanMessage(content=body.message)], "mode": mode},
            config=_config(session.session_id),
        )

        reply = result.get("reply_text", "")
        draft = _current_draft(request, session.session_id)
        turn_id = _turn_id(request, session.session_id)

        set_output(root, reply)
        set_outcome(root, "success")

    return ChatResponse(reply=reply, mode=mode, draft=draft, turn_id=turn_id)


@router.post("/reset", response_model=ResetResponse)
def reset(request: Request) -> ResetResponse:
    session = reset_session()  # rotates session_id → new thread_id → empty state
    with chat_span(
        "brd_agent.reset",
        session_id=session.session_id,
        span_kind=KIND_CHAIN,
        otel_kind=SpanKind.SERVER,
        chat_mode="reset",
        tags=["reset"],
        track_outcome=True,
    ) as span:
        set_input(span, "(reset)")
        set_output(
            span,
            {"status": "ok", "session_id": session.session_id},
            mime="application/json",
        )
        set_outcome(span, "reset")
    return ResetResponse(status="ok", session_id=session.session_id)


@router.get("/memory", response_model=MemoryResponse)
def memory_inspect(request: Request) -> MemoryResponse:
    session = get_session()
    state = _read_graph_state(request, session.session_id)
    mem = state.get("brd_memory")
    # Fallback: read directly from the LangMem store if state is empty
    if mem is None:
        runtime = request.app.state.runtime
        mem = read_memory(runtime.store, session.session_id)
    return MemoryResponse(session_id=session.session_id, memory=mem)


@router.post("/tools/fetch-brd-template", response_model=FetchTemplateResponse)
def fetch_template(body: FetchTemplateBody, request: Request) -> FetchTemplateResponse:
    """Invoke the synthetic fetch_brd_template tool.

    Phase 5 demonstrates the artifact pattern without going through LLM
    tool-calling: we run the tool directly, stash the ref in
    state.last_retrievals.artifact_refs (the assembler reads it on the
    NEXT chat turn), and add a short condensed AIMessage to the transcript
    so the user-visible chat reflects what was fetched. The full content
    never enters the buffer — only the summary surfaces.
    """
    session = get_session()
    runtime = request.app.state.runtime
    graph = request.app.state.gathering_graph

    with chat_span(
        "tool.fetch_brd_template",
        session_id=session.session_id,
        span_kind=KIND_TOOL,
        otel_kind=SpanKind.SERVER,
        chat_mode="tool_call",
        tags=["tool"],
        track_outcome=True,
    ) as span:
        set_input(
            span, {"template_name": body.template_name}, mime="application/json"
        )
        ref = fetch_brd_template(
            template_name=body.template_name,
            store=runtime.artifact_store,
        )
        span.set_attribute("artifact.id", ref.artifact_id)
        span.set_attribute("artifact.type", ref.artifact_type)
        span.set_attribute("artifact.name", ref.name)
        span.set_attribute("artifact.size_bytes", ref.size_bytes)

        # Persist the ref into thread state so the assembler picks it up on
        # the next chat turn. update_state appends to the same
        # `last_retrievals` dict (full overwrite of that field — assembler
        # nodes also write to last_retrievals, but they always read first
        # and merge, see retrieve_context).
        config = _config(session.session_id)
        current = _read_graph_state(request, session.session_id).get("last_retrievals") or {}
        existing_refs = current.get("artifact_refs") or []
        condensed = (
            f"📎 Fetched **{ref.name}** "
            f"(artifact_id={ref.artifact_id[:8]}, {ref.size_bytes}B). "
            f"Summary: {ref.summary}"
        )
        graph.update_state(
            config,
            values={
                "last_retrievals": {
                    **current,
                    "artifact_refs": [*existing_refs, ref.model_dump(mode="json")],
                },
                "messages": [AIMessage(content=condensed)],
            },
        )

        set_output(
            span,
            {"artifact_id": ref.artifact_id, "name": ref.name},
            mime="application/json",
        )
        set_outcome(span, "success")

    return FetchTemplateResponse(
        artifact_ref=ref,
        available_templates=available_templates(),
        note=(
            "Artifact body stashed in ArtifactStore; only the summary will "
            "surface on the next chat turn via the assembled prompt's "
            "TOOL_ARTIFACTS block."
        ),
    )
