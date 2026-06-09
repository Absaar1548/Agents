"""FastAPI app: HTTP surface for the BRD agent.

Thin router — all endpoints live in `backend.api.*` modules.
Lifespan builds the runtime, sources, graphs, and stores them on
`app.state` so every endpoint can access them via `request.app.state`.
"""
from __future__ import annotations

import yaml
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from langgraph.checkpoint.sqlite import SqliteSaver

# Load .env BEFORE telemetry/openai imports inside backend modules.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Ensure persistent storage directory exists before SqliteSaver is imported.
Path("storage").mkdir(exist_ok=True)

from backend.api.chat import router as chat_router
from backend.api.drafting import router as drafting_router
from backend.api.review import router as review_router
from backend.api.upload import router as upload_router
from backend.artifacts import ArtifactStore
from backend.context.assembler import ContextAssembler
from backend.core.graph import AgentRuntime, build_drafting_graph, build_gathering_graph
from backend.llm import create_llm_client
from backend.memory import build_memory_manager
from backend.sources.kg import Neo4jKGSource
from backend.sources.vector_store import ChromaRetrievalSource
from backend.telemetry import (
    AGENT_ID,
    SERVICE_VERSION,
    flush_telemetry,
    init_telemetry,
)


# ----- lifespan -----
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_telemetry()
    llm = create_llm_client()
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

    runtime = AgentRuntime(
        llm=llm,
        memory_manager=manager,
        store=store,
        assembler=assembler,
        artifact_store=artifact_store,
    )

    async with AsyncExitStack() as stack:
        checkpointer = stack.enter_context(
            SqliteSaver.from_conn_string("storage/checkpoints.db")
        )
        gathering_graph = build_gathering_graph(runtime, checkpointer)
        drafting_graph = build_drafting_graph(runtime, checkpointer)

        # Load agent manifest (YAML)
        manifest_path = Path(__file__).parent / "core" / "manifest.yaml"
        try:
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f)
            log.info("Loaded agent manifest from %s", manifest_path)
        except Exception:
            log.warning("Failed to load manifest from %s; using defaults", manifest_path)
            manifest = None

        app.state.runtime = runtime
        app.state.gathering_graph = gathering_graph
        app.state.drafting_graph = drafting_graph
        app.state.store = store
        app.state.docs_source = docs_source
        app.state.manifest = manifest

        yield
        flush_telemetry()


# ----- app -----
app = FastAPI(title="BRD Agent", version=SERVICE_VERSION, lifespan=lifespan)

# ----- routers -----
app.include_router(chat_router)
app.include_router(drafting_router)
app.include_router(review_router)
app.include_router(upload_router)


# ----- health -----
@app.get("/health")
def health(request: Request) -> dict:
    manifest = getattr(request.app.state, "manifest", None)
    if manifest:
        return {
            "ok": True,
            "agent_id": manifest["agent"]["id"],
            "version": manifest["agent"]["version"],
            "capabilities": manifest.get("capabilities"),
            "hitl_gates": manifest.get("hitl_gates"),
            "guardrails": manifest.get("guardrails"),
            "draft_versioning": manifest.get("draft_versioning"),
        }
    # Fallback to telemetry constants
    return {"ok": True, "agent_id": AGENT_ID, "version": SERVICE_VERSION}
