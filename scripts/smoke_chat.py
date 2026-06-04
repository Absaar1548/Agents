"""Phase 2 smoke test: simulate a multi-turn conversation, then draft.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.smoke_chat
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.agent import BRDAgent  # noqa: E402
from backend.llm import AzureOpenAIClient  # noqa: E402
from backend.session import SessionState  # noqa: E402
from backend.telemetry import flush_telemetry, init_telemetry  # noqa: E402


CONVERSATION = [
    "We want to build a customer onboarding tracker for our private wealth advisory team.",
    "The main stakeholders are Sarah Chen (PM), Marcus Wong (compliance lead), and the field advisors. The advisors care most about reducing the 7-day onboarding cycle to under 3 days.",
    "Core needs: capture KYC docs, run AML screening via our existing vendor, route exceptions to compliance, and notify the advisor when an account is funded. P95 status updates under 5 seconds. Must meet SOC 2 controls.",
]


def main() -> None:
    init_telemetry()
    print("telemetry initialized")

    llm = AzureOpenAIClient()
    agent = BRDAgent(llm)
    session = SessionState()
    print(f"session_id={session.session_id}")

    for i, user_msg in enumerate(CONVERSATION, start=1):
        print(f"\n--- turn {i} ---")
        print(f"USER: {user_msg}")
        reply = agent.chat_turn(session, user_msg)
        print(f"ASSISTANT: {reply}")

    print("\n--- drafting ---")
    brd = agent.draft(session)
    print(f"brd_id: {brd.brd_id}")
    print(f"title: {brd.title}")
    print(f"objectives: {len(brd.objectives)}")
    print(f"stakeholders: {len(brd.stakeholders)}")
    print(f"FRs: {len(brd.functional_requirements)} / NFRs: {len(brd.non_functional_requirements)}")

    flush_telemetry()
    print(f"\nflushed. session_id={session.session_id}")


if __name__ == "__main__":
    main()
