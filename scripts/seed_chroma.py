"""Idempotent Chroma seed.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.seed_chroma

Drops + recreates the collection, then loads three classes of docs the
assembler will retrieve into the prompt depending on strategy:

  1. BRD template — single doc describing the BRD output structure
     (used during BRD_GENERATION).
  2. Glossary — short entries on KYC, AML, SOC 2, NFR categories
     (used during INFORMATION_GATHERING and REQUIREMENT_CONSOLIDATION).
  3. Synthetic prior BRDs — 3 mini examples (lending, custody, advisory)
     used during BRD_GENERATION as reference style.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.sources.vector_store import ChromaRetrievalSource  # noqa: E402


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


BRD_TEMPLATE_DOC = """\
Northern Trust BRD Template (v0.1):

A Business Requirements Document captures the business need, scope, and \
measurable outcomes. Standard sections:

- Title and background (2-4 sentences of business context)
- Objectives: measurable business outcomes (not implementation steps)
- Stakeholders: name, role, primary interest
- Functional requirements (FR-001 style): each has title, description, \
priority (low/medium/high/critical), and acceptance_criteria
- Non-functional requirements (NFR-001 style): category, description, \
measurable target (e.g., "P95 latency < 200ms")
- Acceptance criteria (top-level pass conditions for the whole BRD)
- Assumptions, out-of-scope, dependencies
- Risks: description, likelihood, impact, mitigation

Rule: every FR must have at least one acceptance criterion. Top-level \
acceptance criteria must reference measurable outcomes, not vague language."""


GLOSSARY = [
    (
        "kyc",
        "KYC (Know Your Customer): regulatory process to verify client identity, "
        "ownership, source of funds. Typical artifacts: government-issued ID, "
        "proof of address, beneficial-owner declarations. KYC checks are mandatory "
        "at onboarding and refreshed periodically.",
    ),
    (
        "aml",
        "AML (Anti-Money-Laundering): process of detecting transactions or clients "
        "that may be linked to money-laundering or sanctions risk. Includes sanctions "
        "screening (OFAC, EU, UN lists), PEP (Politically Exposed Person) checks, and "
        "transaction-pattern monitoring. AML hits route to compliance for adjudication.",
    ),
    (
        "soc-2",
        "SOC 2: AICPA audit framework covering five trust criteria — security, "
        "availability, processing integrity, confidentiality, privacy. Type II "
        "reports cover a 6-12 month observation window. Key controls: access "
        "reviews, change management, encryption at rest and in transit, "
        "vulnerability management, logging and monitoring.",
    ),
    (
        "nfr-performance",
        "NFR category 'performance' — measurable: response time percentiles "
        "(P50/P95/P99 latency), throughput (req/s), concurrent-user capacity. "
        "Example target: 'P95 status updates < 5 seconds at 1000 concurrent users'.",
    ),
    (
        "nfr-security",
        "NFR category 'security' — measurable: encryption (TLS 1.2+/AES-256), "
        "authentication (MFA), authorization (least-privilege RBAC), data "
        "classification, key rotation cadence. Reference framework: NIST 800-53 / SOC 2.",
    ),
    (
        "nfr-availability",
        "NFR category 'availability' — measurable: uptime SLO (99.9%/99.95%/99.99%), "
        "RPO (recovery point objective), RTO (recovery time objective). "
        "Trade-off documentation should accompany every availability target.",
    ),
    (
        "nfr-compliance",
        "NFR category 'compliance' — measurable: regulatory frameworks the system "
        "must satisfy (SOC 2 Type II, GDPR, FINRA, MiFID II, SEC Rule 17a-4). "
        "Each compliance NFR references at least one specific control or evidence "
        "the system must produce.",
    ),
    (
        "gdpr",
        "GDPR (EU regulation 2016/679): applies when processing EU residents' "
        "personal data. Key obligations: lawful basis for processing, data minimization, "
        "subject access rights (DSAR), right to erasure, breach notification "
        "within 72 hours, data residency considerations.",
    ),
    (
        "pep",
        "PEP (Politically Exposed Person): individuals with prominent public functions "
        "(heads of state, senior politicians, military officers, senior judiciary, "
        "central-bank officials). Onboarding flags require enhanced due diligence; "
        "ongoing monitoring is more frequent than standard clients.",
    ),
    (
        "sanctions",
        "Sanctions screening: matching client and counterparty data against published "
        "denial lists (OFAC SDN, EU consolidated, UN Security Council, UK HMT). "
        "Re-screening cadence is typically weekly or on-list-update. False positives "
        "are common; adjudication workflow is required.",
    ),
]


PRIOR_BRDS = [
    (
        "prior-brd-onboarding-tracker",
        "Prior BRD: Wealth Onboarding Tracker (Private Wealth division). "
        "Objectives: reduce onboarding from 7 days to <3 days; full audit trail. "
        "Stakeholders: Sarah Chen (PM), Marcus Wong (Compliance Lead). "
        "FRs: capture KYC docs; AML screening via vendor REST; route flagged cases; "
        "real-time advisor dashboard. NFRs: P95 status < 5s; SOC 2 Type II. "
        "Risks: AML vendor outage (mitigation: fallback list cache); examiner audit "
        "findings (mitigation: immutable append-only audit log with daily reconciliation).",
    ),
    (
        "prior-brd-loan-portal",
        "Prior BRD: Retail Loan Application Portal. "
        "Objectives: support 1000 concurrent loan applications with P95 latency <1s. "
        "Stakeholders: Priya Patel (Head of Retail Lending), Risk Officers. "
        "FRs: capture borrower data; integrate with core-banking REST API; "
        "support multi-stage approval workflow with reassignment. NFRs: P95 latency <1s; "
        "1000 concurrent users; SOC 2; data residency EU. "
        "Out-of-scope: CRM integration (deferred to v2). Dependencies: Okta SSO; "
        "Azure Blob (EU) for document storage.",
    ),
    (
        "prior-brd-custody-recon",
        "Prior BRD: Custody Reconciliation Dashboard. "
        "Objectives: surface intraday position breaks within 15 minutes of cycle close. "
        "Stakeholders: Custody Ops, Internal Audit, External Auditors (read-only). "
        "FRs: ingest holdings from custody platform; compare against fund accounting; "
        "categorize breaks (timing/amount/position); export audit pack. "
        "NFRs: reconciliation pipeline <10 minutes; 7-year WORM retention of pack outputs; "
        "encryption AES-256 at rest. Risks: source-system schema drift; mitigation: "
        "contract tests on every ingestion job.",
    ),
]


def main() -> None:
    src = ChromaRetrievalSource()
    print(f"persist_dir: {src.persist_dir}")
    print(f"collection : {src.collection_name}")

    src.reset_collection()
    print("collection reset → 0 items")

    src.add(
        doc_id="brd-template-v0.1",
        text=BRD_TEMPLATE_DOC,
        metadata={"kind": "template", "version": "0.1"},
    )

    for key, body in GLOSSARY:
        src.add(
            doc_id=f"glossary-{key}",
            text=body,
            metadata={"kind": "glossary", "term": key},
        )

    for key, body in PRIOR_BRDS:
        src.add(
            doc_id=key,
            text=body,
            metadata={"kind": "prior_brd", "title": key.replace("prior-brd-", "")},
        )

    total = src.count()
    print(f"seeded → {total} items "
          f"(1 template + {len(GLOSSARY)} glossary + {len(PRIOR_BRDS)} prior BRDs)")

    # Quick retrieval sanity check
    sample_q = "What does SOC 2 require for compliance?"
    hits = src.retrieve(query=sample_q, k=3)
    print(f"\nsanity query: {sample_q!r}")
    for h in hits:
        print(f"  - id={h.id}  distance={h.distance:.4f}  kind={h.metadata.get('kind')}")
        print(f"    {h.text[:120]}...")


if __name__ == "__main__":
    main()
