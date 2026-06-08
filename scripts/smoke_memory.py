"""Phase 3 smoke test: LangMem extracts BRD info across turns.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.smoke_memory
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from backend.telemetry import flush_telemetry, init_telemetry  # noqa: E402
from backend.llm import create_llm_client  # noqa: E402
from backend.memory import build_memory_manager, read_memory  # noqa: E402
from backend.context.assembler import ContextAssembler  # noqa: E402
from backend.artifacts import ArtifactStore  # noqa: E402
from backend.core.graph import AgentRuntime, build_gathering_graph  # noqa: E402
from backend.sources.vector_store import ChromaRetrievalSource  # noqa: E402
from backend.sources.kg import Neo4jKGSource  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402


CONVERSATION = [
    "We need a customer onboarding tracker for private wealth advisory.",
    "Sarah Chen is the PM. Marcus Wong leads compliance.",
    "Must capture KYC docs, run AML screening, and notify advisors when accounts fund. P95 status updates under 5 seconds. SOC 2 required.",
]


def main() -> None:
    init_telemetry()
    print("telemetry initialized")

    llm = create_llm_client()
    store, manager = build_memory_manager()

    docs_source = ChromaRetrievalSource()
    kg_source = Neo4jKGSource()
    if not kg_source.verify():
        print(f"Neo4j at {kg_source.uri} unreachable; running without KG")
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

    gathering_graph = build_gathering_graph(runtime)

    thread_id = "smoke-memory-test"
    config = {"configurable": {"thread_id": thread_id}}
    print(f"thread_id={thread_id}")

    for i, msg in enumerate(CONVERSATION, start=1):
        print(f"\n--- turn {i} ---")
        print(f"USER: {msg}")
        result = gathering_graph.invoke(
            {"messages": [HumanMessage(content=msg)], "mode": "gathering"},
            config=config,
        )
        reply = result.get("reply_text", "")
        print(f"ASSISTANT: {reply[:150]}...")

        mem = read_memory(store, thread_id)
        if mem:
            print(f"MEMORY after turn {i}:")
            print(json.dumps(mem, indent=2, default=str)[:600])
        else:
            print(f"MEMORY after turn {i}: (empty)")

    flush_telemetry()
    print(f"\nflushed. thread_id={thread_id}")


if __name__ == "__main__":
    main()
