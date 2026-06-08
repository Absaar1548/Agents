"""Phase 2 smoke test: simulate a multi-turn conversation, then draft.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.smoke_chat
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from backend.telemetry import flush_telemetry, init_telemetry  # noqa: E402
from backend.llm import create_llm_client  # noqa: E402
from backend.memory import build_memory_manager  # noqa: E402
from backend.context.assembler import ContextAssembler  # noqa: E402
from backend.artifacts import ArtifactStore  # noqa: E402
from backend.core.graph import AgentRuntime, build_drafting_graph, build_gathering_graph  # noqa: E402
from backend.sources.vector_store import ChromaRetrievalSource  # noqa: E402
from backend.sources.kg import Neo4jKGSource  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402


CONVERSATION = [
    "We want to build a customer onboarding tracker for our private wealth advisory team.",
    "The main stakeholders are Sarah Chen (PM), Marcus Wong (compliance lead), and the field advisors. The advisors care most about reducing the 7-day onboarding cycle to under 3 days.",
    "Core needs: capture KYC docs, run AML screening via our existing vendor, route exceptions to compliance, and notify the advisor when an account is funded. P95 status updates under 5 seconds. Must meet SOC 2 controls.",
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
    drafting_graph = build_drafting_graph(runtime)

    thread_id = "smoke-chat-test"
    config = {"configurable": {"thread_id": thread_id}}
    print(f"thread_id={thread_id}")

    for i, user_msg in enumerate(CONVERSATION, start=1):
        print(f"\n--- turn {i} ---")
        print(f"USER: {user_msg}")
        result = gathering_graph.invoke(
            {"messages": [HumanMessage(content=user_msg)], "mode": "gathering"},
            config=config,
        )
        reply = result.get("reply_text", "")
        print(f"ASSISTANT: {reply}")

    print("\n--- drafting ---")
    result = drafting_graph.invoke({"mode": "drafting"}, config=config)
    draft_dict = result.get("current_draft")
    if draft_dict is None:
        print("ERROR: drafting_graph did not produce a current_draft")
        return

    from backend.schema import BRDResponse  # noqa: E402
    brd = BRDResponse.model_validate(draft_dict)
    print(f"brd_id: {brd.brd_id}")
    print(f"title: {brd.title}")
    print(f"objectives: {len(brd.objectives)}")
    print(f"stakeholders: {len(brd.stakeholders)}")
    print(f"FRs: {len(brd.functional_requirements)} / NFRs: {len(brd.non_functional_requirements)}")

    flush_telemetry()
    print(f"\nflushed. thread_id={thread_id} — check Phoenix at http://localhost:6006")


if __name__ == "__main__":
    main()
