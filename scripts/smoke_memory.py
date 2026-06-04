"""Phase 3 smoke test: LangMem extracts BRD info across turns.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.smoke_memory
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.agent import BRDAgent  # noqa: E402
from backend.llm import AzureOpenAIClient  # noqa: E402
from backend.memory import build_memory_manager, read_memory  # noqa: E402
from backend.session import SessionState  # noqa: E402
from backend.telemetry import flush_telemetry, init_telemetry  # noqa: E402


CONVERSATION = [
    "We need a customer onboarding tracker for private wealth advisory.",
    "Sarah Chen is the PM. Marcus Wong leads compliance.",
    "Must capture KYC docs, run AML screening, and notify advisors when accounts fund. P95 status updates under 5 seconds. SOC 2 required.",
]


def main() -> None:
    init_telemetry()
    print("telemetry initialized")

    llm = AzureOpenAIClient()
    store, manager = build_memory_manager()
    agent = BRDAgent(llm, store=store, memory_manager=manager)
    session = SessionState()
    print(f"session_id={session.session_id}")

    for i, msg in enumerate(CONVERSATION, start=1):
        print(f"\n--- turn {i} ---")
        print(f"USER: {msg}")
        reply = agent.chat_turn(session, msg)
        print(f"ASSISTANT: {reply[:150]}...")

        mem = read_memory(store, session.session_id)
        if mem:
            print(f"MEMORY after turn {i}:")
            print(json.dumps(mem, indent=2, default=str)[:600])
        else:
            print(f"MEMORY after turn {i}: (empty)")

    print("\n--- drafting from gathered memory ---")
    brd = agent.draft(session)
    print(f"brd_id: {brd.brd_id}, title: {brd.title}")
    print(f"FRs: {len(brd.functional_requirements)} / NFRs: {len(brd.non_functional_requirements)}")
    print(f"stakeholders: {[s.name for s in brd.stakeholders]}")

    flush_telemetry()
    print(f"\nflushed. session_id={session.session_id}")


if __name__ == "__main__":
    main()
