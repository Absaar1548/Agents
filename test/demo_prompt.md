# Demo starting prompt — BRD Agent

Paste the block below as your **first message** in the Streamlit chat input
at `http://localhost:8501`. It's a realistic Northern Trust–flavoured scenario
that seeds objectives, stakeholders, functional + non-functional requirements,
constraints, and out-of-scope all at once — so the agent has rich context to
ask sharper follow-up questions instead of starting from a blank page.

## Paste this into the chat

```text
We need a Client Onboarding Tracker for our Private Wealth Advisory team.

Today onboarding takes an average of 7 business days; Sarah Chen (PM)
wants to bring that under 3. Marcus Wong leads compliance and needs full
audit traceability of every KYC document collected, every AML screening
result, and every exception routed to compliance review. Field advisors
want to be notified the moment an account is funded.

The tracker must capture KYC docs, run AML screening through our existing
vendor (REST integration), route flagged cases to compliance, and surface
a real-time status dashboard for advisors and the PM. P95 status updates
should be under 5 seconds and the system must meet SOC 2 controls. We
don't yet have CRM integration scoped — assume it's out of scope for v1.
```

## What this exercises

| BRD section | Seeded by |
| --- | --- |
| `title` | "Client Onboarding Tracker for Private Wealth Advisory" |
| `objectives` | 7-day → <3-day onboarding; full audit traceability; advisor-fund-event notifications |
| `stakeholders` | Sarah Chen (PM), Marcus Wong (Compliance Lead), field advisors |
| `functional_requirements` | Capture KYC, AML screening via vendor, route flagged cases, real-time dashboard |
| `non_functional_requirements` | P95 status updates < 5s; SOC 2 compliance |
| `dependencies` | Existing AML vendor (REST integration) |
| `out_of_scope` | CRM integration (v1) |

## Suggested follow-up turns

After the agent responds to the first paste, try these one at a time to
fill in the gaps it asks about:

1. **Risk + likelihood** — "Main risk we're worried about is an examiner finding gaps in audit evidence; likelihood medium, impact high. Mitigation is immutable append-only audit log with daily reconciliation."
2. **Priorities** — "FRs should be prioritised: KYC capture and AML screening are critical; advisor notifications are high; dashboard is medium for v1."
3. **Acceptance criteria** — "For KYC capture, acceptance is: advisor can upload PDF/JPG, system OCRs and surfaces extracted fields for confirmation, document is hashed and timestamped."

Then click **🪄 Generate BRD**, review the inline draft, and either:

- click **✅ Approve** to lock the `brd_id`, or
- type a change request (e.g. "Add a risk about vendor-API downtime") in
  the "Request changes" box, then click **🔄 Regenerate BRD**.

## Where to watch the traces

Phoenix at `http://localhost:6006`. Filter `service.name = brd-agent`.
Each user action produces one trace; expand to see the span tree:

```
chat.turn  ├─ memory.fetch   prompt.assemble   agent.invoke (+ChatCompletion)   memory.update (+ChatCompletion)
chat.draft ├─ memory.fetch   prompt.assemble   draft.llm_call (+ChatCompletion)   draft.schema_validate
```
