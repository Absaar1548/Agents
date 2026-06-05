# State Flow Diagram — BRD Agent (3-State + HITL Gates)

This diagram models the BRD Agent as a **3-state machine** with **2 Human-in-the-Loop (HITL) gates**. States are defined by mode + `feedback_gathering` + `current_draft` + `session.approved`.

```mermaid
stateDiagram-v2
    [*] --> AgentInteraction: User starts chat

    AgentInteraction: 🟡 Agent Interaction
    AgentInteraction: feedback_gathering = false
    AgentInteraction: current_draft = null
    AgentInteraction: approved = false
    note right of AgentInteraction
        Universal entry point.
        Ingest docs, ask clarifying
        questions, collect ALL
        feedback before exiting.
        Includes feedback gathering
        sub-loop when re-entering
        from Review Mode.
    end note

    AgentInteraction --> AgentInteraction: /chat (answer question)
    AgentInteraction --> AgentInteraction: /request-changes (more feedback)
    AgentInteraction --> HITL1: Auto-transition<br/>"Sufficient detail gathered? = YES"
    AgentInteraction --> HITL1: Feedback complete<br/>user said "no more reviews"

    HITL1: 👤 HITL 1:
    HITL1: Review Summary &
    HITL1: Proceed to Production?
    note right of HITL1
        Agent presents summary
        of gathered requirements
        or collected feedback.
        User must explicitly
        confirm before drafting.
        Prevents premature BRD.
    end note

    HITL1 --> BRDProduction: User confirms "Yes, proceed"
    HITL1 --> AgentInteraction: User says "No, need more info"

    BRDProduction: 🟢 BRD Production
    BRDProduction: mode = "drafting"
    BRDProduction: current_draft = BRDResponse (being written)
    BRDProduction: approved = false
    note right of BRDProduction
        Atomic generate OR update.
        Includes ALL gathered
        context + pending_feedback.
        Single-shot graph.
        Auto-transitions to Review
        on success.
    end note

    BRDProduction --> ReviewMode: Auto-transition<br/>(on success)
    BRDProduction --> AgentInteraction: On failure<br/>(error → retry)

    ReviewMode: 🔵 Review Mode (HITL 2)
    ReviewMode: mode = "awaiting_approval"
    ReviewMode: current_draft = BRDResponse
    ReviewMode: approved = false
    note right of ReviewMode
        User reviews BRD.
        HITL 2 decision:
        1. Accept → END
        2. Changes → feedback gathering
    end note

    ReviewMode --> Approved: POST /approve (HITL 2 = Accept)
    ReviewMode --> AgentInteraction: POST /request-changes (HITL 2 = Changes)

    Approved: ✅ Approved
    Approved: mode = "approved"
    Approved: current_draft = BRDResponse
    Approved: approved = true
    note right of Approved
        Final state.
        BRD is locked.
        Session can be reset
        for a new BRD.
    end note

    Approved --> [*]: /reset
```

## State Definition Table

A state is uniquely identified by the tuple: **(mode, feedback_gathering, current_draft, approved)**

| State Name | mode | feedback_gathering | current_draft | approved | Entry From | Exit To | HITL |
|---|---|---|---|---|---|---|---|
| **🟡 Agent Interaction** | `gathering` | `false` | `null` | `false` | `*` (start) / HITL1 (No) / ReviewMode (changes) | HITL1 (auto) | — |
| **🟡 Agent Interaction (Feedback Sub-loop)** | `request_changes` | `true` | `BRDResponse` | `false` | ReviewMode (changes) | HITL1 (feedback complete) | — |
| **👤 HITL 1** | `gathering` | `false` | `null` or `BRDResponse` | `false` | 🟡 Agent Interaction (ready) | 🟢 Production (Yes) / 🟡 Agent Interaction (No) | Gate before Production |
| **🟢 BRD Production** | `drafting` | `false` | `BRDResponse` (writing) | `false` | HITL1 (Yes) | 🔵 Review (auto) | — |
| **🔵 Review Mode (HITL 2)** | `awaiting_approval` | `false` | `BRDResponse` | `false` | 🟢 Production | ✅ Approved (accept) / 🟡 Agent Interaction (changes) | Gate after Production |
| **✅ Approved** | `approved` | `false` | `BRDResponse` | `true` | 🔵 Review (accept) | `*` (reset) | — |

## Design Rules

1. **🟡 Agent Interaction is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic per cycle** — Within each production cycle, the BRD is produced/updated **all at once** with the complete gathered context. Multiple cycles are allowed until the user is satisfied.
3. **Feedback re-entry is not a separate mode** — it is the same Agent Interaction loop, entered with the current draft in context. The user may attach supporting documents with feedback, and the loop ends at the same HITL 1 gate.
4. **HITL 1 is the single per-cycle approval** — for both generation and updates. The agent cannot auto-jump to 🟢 Production; it must pause and present a summary (gathered requirements for a new BRD, or the concrete planned change for an update) and get explicit approval. The user either proceeds or adds more feedback. This prevents premature drafting.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from HITL 2 is the only backward transition triggered by the user.
7. **The Knowledge Graph is a pre-seeded enterprise KB** — Ingestion populates the Vector DB only; the agent reads enterprise context from the KG but does not write to it. Enriching the KG from ingested documents is a future option.

## Draft Lifecycle with Feedback

```
null → HITL1 → BRDResponse (v1) → HITL2 → HITL1 → BRDResponse (v2) → ... → Approved
       ^                         ^
       |                         |
    generate-brd              auto after
    (initial)                 feedback complete
```

**Current behavior:** `current_draft` is overwritten on every re-generation, but `pending_feedback` preserves the set of changes that triggered the update. No version history is kept in state.
