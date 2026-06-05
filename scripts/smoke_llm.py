"""Phase 1 smoke test: init telemetry, call Azure OpenAI, flush.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.smoke_llm
"""
from __future__ import annotations

import uuid
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE importing telemetry/llm (they read env at construction).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.telemetry import chat_span, flush_telemetry, init_telemetry  # noqa: E402
from backend.llm import create_llm_client  # noqa: E402
from backend.schema import (  # noqa: E402
    BRDResponse,
    FunctionalRequirement,
    NFRCategory,
    NonFunctionalRequirement,
    Priority,
    Stakeholder,
)


def main() -> None:
    init_telemetry()
    print("telemetry initialized")

    llm = create_llm_client()
    session_id = uuid.uuid4().hex

    with chat_span("smoke.llm_roundtrip", session_id=session_id, turn_id=1):
        text = llm.complete(
            messages=[
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": "Say hello and name yourself in one sentence."},
            ],
            max_tokens=500,
        )
    print(f"LLM reply: {text!r}")

    sample = BRDResponse(
        title="Demo BRD",
        background="Smoke test fixture.",
        objectives=["Validate the schema accepts a hand-crafted dict."],
        stakeholders=[Stakeholder(name="Absaar", role="Engineer", interest="PoC")],
        functional_requirements=[
            FunctionalRequirement(
                id="FR-001",
                title="Roundtrip",
                description="Schema accepts a hand-built FR.",
                priority=Priority.MEDIUM,
                acceptance_criteria=["Validates without error"],
            )
        ],
        non_functional_requirements=[
            NonFunctionalRequirement(
                id="NFR-001",
                category=NFRCategory.MAINTAINABILITY,
                description="Schema stays under 100 lines.",
                target="< 100 LOC",
            )
        ],
        acceptance_criteria=["smoke_llm.py exits 0"],
        drafted_by="brd-agent@0.1.0",
    )
    print(f"BRDResponse roundtrip OK: brd_id={sample.brd_id}")

    flush_telemetry()
    print(f"flushed. session_id={session_id} — check Phoenix at http://localhost:6006")


if __name__ == "__main__":
    main()
