"""Presidio guardrails stub — Phase 3 implementation.

Integration points (4):
  1. Before invoke_llm (scan user message)
  2. After invoke_llm (scan reply)
  3. Before draft_llm (scan assembled context)
  4. After schema_validate (scan generated BRD)
"""
from __future__ import annotations

from typing import Any


# Entities to actively MASK in prompts and outputs
MASK_ENTITIES = [
    "CREDIT_CARD",
    "US_SSN",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_BANK_NUMBER",
    "IBAN_CODE",
]

# Entities to LOG but NOT mask (legitimate in BRD context)
LOG_ONLY_ENTITIES = ["PERSON", "IP_ADDRESS"]

ALL_ENTITIES = MASK_ENTITIES + LOG_ONLY_ENTITIES


def scan_and_protect(text: str) -> tuple[str, list[dict], list[dict]]:
    """Scan text for PII.

    Returns (cleaned_text, masked_findings, logged_findings).
    Stub implementation — returns input unchanged.
    """
    return text, [], []
