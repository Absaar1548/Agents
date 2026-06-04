"""FastAPI app: HTTP surface for the BRD agent.

Phase 1 architecture:
  - State lives in LangGraph's per-thread checkpointer (MemorySaver).
  - Endpoints invoke one of two compiled graphs:
       /chat, /request-changes → gathering_graph
       /generate-brd          → drafting_graph
  - SessionState (backend/session.py) is now thin: session_id (== thread_id)
    + approved flag. Messages, mode, current_draft, brd_memory all live
    in graph state.
  - Root telemetry spans (brd_agent.turn / .draft / .request_changes /
    .approve / .reset) wrap graph.invoke here in main.py.

HTTP contract is unchanged from the Phase 0 chatbot — the Streamlit
frontend works as-is.

Endpoints:
  POST /chat              { message }    → { reply, mode, draft?, turn_id }
  POST /generate-brd      {}             → { draft, mode }
  POST /approve           {}             → { status, brd_id }
  POST /request-changes   { feedback }   → { reply, draft, mode }
  POST /reset             {}             → { status, session_id }
  GET  /memory                           → { session_id, memory }
  GET  /health                           → { ok, agent_id, version }
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

# Load .env BEFORE telemetry/openai imports inside backend modules.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.artifacts import ArtifactRef, ArtifactStore  # noqa: E402
from backend.context_assembler import ContextAssembler  # noqa: E402
from backend.graph import (  # noqa: E402
    AgentRuntime,
    build_drafting_graph,
    build_gathering_graph,
    checkpointer,
)
from backend.sources.kg import Neo4jKGSource  # noqa: E402
from backend.sources.vector_store import ChromaRetrievalSource  # noqa: E402
from backend.tools.fetch_brd_template import (  # noqa: E402
    available_templates,
    fetch_brd_template,
)
from langchain_core.messages import AIMessage  # noqa: E402
from backend.llm import AzureOpenAIClient  # noqa: E402
from backend.memory import build_memory_manager, read_memory  # noqa: E402
from backend.schema import BRDResponse  # noqa: E402
from backend.session import get_session, reset_session  # noqa: E402
from backend.telemetry import (  # noqa: E402
    AGENT_ID,
    KIND_AGENT,
    KIND_CHAIN,
    KIND_TOOL,
    SERVICE_VERSION,
    chat_span,
    flush_telemetry,
    init_telemetry,
    set_input,
    set_outcome,
    set_output,
)


# ----- runtime + graphs (built in lifespan) -----
_runtime: Optional[AgentRuntime] = None
_gathering_graph = None
_drafting_graph = None


# ----- request / response models (HTTP contract — unchanged) -----
class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    mode: str
    draft: Optional[BRDResponse] = None
    turn_id: int


class DraftResponse(BaseModel):
    draft: BRDResponse
    mode: str


class ApproveResponse(BaseModel):
    status: str
    brd_id: str


class RequestChangesBody(BaseModel):
    feedback: str = Field(min_length=1)


class RequestChangesResponse(BaseModel):
    reply: str
    draft: BRDResponse
    mode: str


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


# ----- helpers -----
def _config(thread_id: str) -> dict:
    """The RunnableConfig handed to every graph invocation."""
    return {"configurable": {"thread_id": thread_id}}


def _read_graph_state(thread_id: str) -> dict:
    """Pull the current persisted state for a thread (empty dict if none)."""
    snapshot = _gathering_graph.get_state(_config(thread_id))
    return snapshot.values if snapshot else {}


def _turn_id(thread_id: str) -> int:
    """Best-effort monotonic turn id derived from #HumanMessages in state."""
    state = _read_graph_state(thread_id)
    messages = state.get("messages") or []
    return sum(1 for m in messages if isinstance(m, HumanMessage))


def _current_draft(thread_id: str) -> Optional[BRDResponse]:
    state = _read_graph_state(thread_id)
    raw = state.get("current_draft")
    return BRDResponse.model_validate(raw) if raw else None


# ----- lifespan -----
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_telemetry()
    llm = AzureOpenAIClient()
    store, manager = build_memory_manager()

    # Docs source (Phase 3). If the Chroma collection is empty, log a warning
    # — the assembler will simply skip doc retrieval until seed_chroma runs.
    docs_source = ChromaRetrievalSource()
    import logging
    log = logging.getLogger(__name__)
    if docs_source.count() == 0:
        log.warning(
            "Chroma collection '%s' is empty. Run: "
            ".venv/bin/python -m scripts.seed_chroma",
            docs_source.collection_name,
        )

    # KG source (Phase 4). Soft-fail: if Neo4j is unreachable, run without
    # the KG layer — the assembler skips kg retrieval when kg_source is None.
    kg_source = Neo4jKGSource()
    if not kg_source.verify():
        log.warning(
            "Neo4j at %s unreachable; running without KG. "
            "Start it via: (cd infra && docker compose up -d neo4j)",
            kg_source.uri,
        )
        kg_source = None

    assembler = ContextAssembler(docs_source=docs_source, kg_source=kg_source)
    artifact_store = ArtifactStore()

    global _runtime, _gathering_graph, _drafting_graph
    _runtime = AgentRuntime(
        llm=llm,
        memory_manager=manager,
        store=store,
        assembler=assembler,
        artifact_store=artifact_store,
    )
    _gathering_graph = build_gathering_graph(_runtime)
    _drafting_graph = build_drafting_graph(_runtime)

    app.state.store = store
    yield
    flush_telemetry()


app = FastAPI(title="BRD Agent", version=SERVICE_VERSION, lifespan=lifespan)


# ----- endpoints -----
@app.get("/health")
def health() -> dict:
    return {"ok": True, "agent_id": AGENT_ID, "version": SERVICE_VERSION}


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    session = get_session()
    existing_draft = _current_draft(session.session_id)

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

        result = _gathering_graph.invoke(
            {"messages": [HumanMessage(content=body.message)], "mode": mode},
            config=_config(session.session_id),
        )

        reply = result.get("reply_text", "")
        draft = _current_draft(session.session_id)
        turn_id = _turn_id(session.session_id)

        set_output(root, reply)
        set_outcome(root, "success")

    return ChatResponse(reply=reply, mode=mode, draft=draft, turn_id=turn_id)


@app.post("/generate-brd", response_model=DraftResponse)
def generate_brd() -> DraftResponse:
    session = get_session()
    with chat_span(
        "brd_agent.draft",
        session_id=session.session_id,
        span_kind=KIND_AGENT,
        otel_kind=SpanKind.SERVER,
        chat_mode="drafting",
        tags=["drafting"],
        track_outcome=True,
    ) as root:
        result = _drafting_graph.invoke(
            {"mode": "drafting"},
            config=_config(session.session_id),
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


@app.post("/approve", response_model=ApproveResponse)
def approve() -> ApproveResponse:
    session = get_session()
    draft = _current_draft(session.session_id)
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


@app.post("/request-changes", response_model=RequestChangesResponse)
def request_changes(body: RequestChangesBody) -> RequestChangesResponse:
    session = get_session()
    draft = _current_draft(session.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to revise.")

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

        result = _gathering_graph.invoke(
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


@app.post("/reset", response_model=ResetResponse)
def reset() -> ResetResponse:
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


@app.post("/tools/fetch-brd-template", response_model=FetchTemplateResponse)
def fetch_template(body: FetchTemplateBody) -> FetchTemplateResponse:
    """Invoke the synthetic fetch_brd_template tool.

    Phase 5 demonstrates the artifact pattern without going through LLM
    tool-calling: we run the tool directly, stash the ref in
    state.last_retrievals.artifact_refs (the assembler reads it on the
    NEXT chat turn), and add a short condensed AIMessage to the transcript
    so the user-visible chat reflects what was fetched. The full content
    never enters the buffer — only the summary surfaces.
    """
    session = get_session()
    runtime = get_agent_runtime()

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
        current = _read_graph_state(session.session_id).get("last_retrievals") or {}
        existing_refs = current.get("artifact_refs") or []
        condensed = (
            f"📎 Fetched **{ref.name}** "
            f"(artifact_id={ref.artifact_id[:8]}, {ref.size_bytes}B). "
            f"Summary: {ref.summary}"
        )
        _gathering_graph.update_state(
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


def get_agent_runtime() -> AgentRuntime:
    if _runtime is None:
        raise RuntimeError("Agent runtime not initialized — lifespan didn't run")
    return _runtime


@app.get("/memory", response_model=MemoryResponse)
def memory_inspect() -> MemoryResponse:
    session = get_session()
    state = _read_graph_state(session.session_id)
    mem = state.get("brd_memory")
    # Fallback: read directly from the LangMem store if state is empty
    if mem is None and _runtime is not None:
        mem = read_memory(_runtime.store, session.session_id)
    return MemoryResponse(session_id=session.session_id, memory=mem)
