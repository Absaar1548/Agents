"""fetch_brd_template — synthetic tool exercising the artifact pattern.

Returns the full body of a named template (kept here verbatim for the
PoC; production would pull from a doc repo / Confluence / SharePoint).
The tool stashes the body in the ArtifactStore and returns the
ArtifactRef so the caller can:
  - put the ref into state.last_retrievals.artifact_refs (the assembler
    picks it up on the NEXT turn),
  - put a short ToolMessage-shaped summary into the conversation buffer
    so the LLM sees what was fetched without the full payload.

If we later wire LangGraph tool-calling end-to-end (LLM decides to call
the tool), this same function plugs in directly as the tool's callable.
"""
from __future__ import annotations

from typing import Optional

from backend.artifacts import ArtifactRef, ArtifactStore


# Synthetic template bodies — large enough to make the artifact pattern
# meaningful but small enough to keep this file readable. ~2-3 KB each.
_TEMPLATES: dict[str, dict[str, str]] = {
    "default": {
        "summary": (
            "Default BRD template — sections, ordering, NFR category guidance, "
            "acceptance-criteria rules, risk schema."
        ),
        "body": """\
Northern Trust BRD Template — full structure (v0.1)

1. Title
   Short noun phrase. Includes business division and a verb (e.g. \
"Custody Reconciliation Dashboard — Modernise Intraday Position Breaks").

2. Background
   2-4 sentences. WHY this work exists — what business condition or \
regulatory pressure prompted it. Avoid implementation detail; stick to \
business framing.

3. Objectives (measurable outcomes)
   3-5 statements. Each MUST include a measurable target. \
Examples:
     - "Reduce average onboarding cycle from 7 to <3 business days"
     - "Surface intraday position breaks within 15 minutes of cycle close"
   Anti-pattern: "Improve compliance" (not measurable).

4. Stakeholders
   Each entry: name, role, primary interest. List BOTH active sponsors \
(PM, product owners) AND consulted parties (compliance, audit, infra).

5. Functional Requirements (FR-001 style)
   Each FR has:
     - id        (FR-001, FR-002, ...)
     - title     (verb phrase)
     - description (1-3 sentences)
     - priority  (low | medium | high | critical)
     - acceptance_criteria (list, MIN ONE entry; written so QA can verify)
   Rule: every FR has at least one acceptance criterion. FRs without \
acceptance criteria are placeholders, not requirements.

6. Non-Functional Requirements (NFR-001 style)
   Each NFR has:
     - id, category, description, target (measurable)
   Standard categories:
     performance    P95/P99 latency, throughput, concurrent capacity
     security       encryption (TLS 1.2+/AES-256), MFA, RBAC, key rotation
     availability   uptime SLO (99.9/99.95/99.99), RPO, RTO
     scalability    growth assumptions, horizontal-scale ceilings
     compliance     SOC 2, GDPR, FINRA, MiFID II, SEC 17a-4
     maintainability deploy cadence, runbook coverage, on-call rotation
     usability      task completion time, error recovery paths

7. Acceptance criteria (BRD-level)
   Top-level pass conditions for the whole document. Same measurability \
rule as objectives.

8. Assumptions
   Things being taken as given. Each assumption surfaces a risk if it \
later turns out false — document the fall-back.

9. Out-of-scope
   Explicitly carved out. Helps reviewers spot scope creep.

10. Dependencies
    Upstream systems, third-party vendors, infrastructure prerequisites.

11. Risks
    Each risk: description, likelihood, impact, mitigation. Likelihood \
and impact: low | medium | high | critical.

Drafting rules:
  - Every FR has at least one acceptance criterion (hard rule).
  - Objectives reference measurable outcomes, not implementation steps.
  - Risks always include a mitigation.
  - When in doubt, list it in `assumptions` rather than `out_of_scope`.
""",
    },
    "compliance": {
        "summary": (
            "Compliance-focused BRD template — same structure as default plus "
            "regulatory-mapping section, evidence-retention table, control matrix."
        ),
        "body": """\
Compliance-focused BRD template (v0.1)

Use this variant when the work has regulatory impact (SOC 2, GDPR, AML, \
KYC, FINRA, SEC 17a-4). It extends the default template with three \
additional sections.

Sections 1-11: same as default template.

12. Regulatory mapping
    For each in-scope regulation, list:
      - regulation name + clause / section
      - control or requirement this BRD satisfies
      - evidence the system must produce
    Example: SOC 2 CC6.1 (logical access) → MFA enforced for admin paths; \
evidence: Okta event logs, retained 7 years.

13. Evidence retention table
    Each artifact this BRD's system produces:
      - artifact type (audit log, screening result, decision record, ...)
      - retention period (e.g. 7 years WORM)
      - storage location (Bronze/Silver/Gold tier or named vault)
      - access controls

14. Control matrix
    Map each NFR.security / NFR.compliance to:
      - control framework (SOC 2 / NIST 800-53)
      - control id
      - implementation responsibility (engineering, compliance, both)

Drafting rules in addition to the default:
  - Every regulatory-mapping row MUST cite specific clause and evidence.
  - Risks must include a regulatory dimension when applicable (e.g. \
"examiner audit finding — likelihood medium, impact high").
  - Stakeholders must include at least one compliance representative.
""",
    },
}


def fetch_brd_template(
    *,
    template_name: Optional[str] = None,
    store: ArtifactStore,
) -> ArtifactRef:
    """Return an ArtifactRef pointing at the named template's body.

    Template lookup is case-insensitive. Unknown name → "default".
    The full body lives in the ArtifactStore keyed by ref.artifact_id;
    callers fetch via store.get_content(ref.artifact_id) when they
    actually need the bytes.
    """
    key = (template_name or "default").lower().strip()
    if key not in _TEMPLATES:
        key = "default"
    entry = _TEMPLATES[key]
    return store.put(
        artifact_type="BRD_TEMPLATE",
        name=f"brd-template-{key}",
        content=entry["body"],
        summary=entry["summary"],
    )


def available_templates() -> list[str]:
    return list(_TEMPLATES.keys())
