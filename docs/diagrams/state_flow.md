# State Flow Diagram — BRD Agent (3-State + HITL Gates)

This diagram models the BRD Agent as a **3-state machine** with **2 Human-in-the-Loop (HITL) gates**. States are defined by mode + `feedback_gathering` + `current_draft` + `session.approved`.

```mermaid
stateDiagram-v2
    [*] --> InformationGathering: User starts chat

    InformationGathering: 🟡 Information Gathering
    InformationGathering: feedback_gathering = false
    InformationGathering: current_draft = null
    InformationGathering: approved = false
    note right of InformationGathering
        Universal entry point.
        Ingest docs, ask clarifying
        questions, collect ALL
        feedback before exiting.
        Includes feedback gathering
        sub-loop when re-entering
        from Review Mode.
    end note

    InformationGathering --> InformationGathering: /chat (answer question)
    InformationGathering --> InformationGathering: /request-changes (more feedback)
    InformationGathering --> HITL1: Auto-transition<br/>"Enough Info? = YES"
    InformationGathering --> HITL1: Feedback complete<br/>user said "no more reviews"

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
    HITL1 --> InformationGathering: User says "No, need more info"

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
    BRDProduction --> InformationGathering: On failure<br/>(error → retry)

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
    ReviewMode --> InformationGathering: POST /request-changes (HITL 2 = Changes)

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
| **🟡 Information Gathering** | `gathering` | `false` | `null` | `false` | `*` (start) / HITL1 (No) / ReviewMode (changes) | HITL1 (auto) | — |
| **🟡 Information Gathering (Feedback Sub-loop)** | `request_changes` | `true` | `BRDResponse` | `false` | ReviewMode (changes) | HITL1 (feedback complete) | — |
| **👤 HITL 1** | `gathering` | `false` | `null` or `BRDResponse` | `false` | 🟡 Gathering (ready) | 🟢 Production (Yes) / 🟡 Gathering (No) | Gate before Production |
| **🟢 BRD Production** | `drafting` | `false` | `BRDResponse` (writing) | `false` | HITL1 (Yes) | 🔵 Review (auto) | — |
| **🔵 Review Mode (HITL 2)** | `awaiting_approval` | `false` | `BRDResponse` | `false` | 🟢 Production | ✅ Approved (accept) / 🟡 Gathering (changes) | Gate after Production |
| **✅ Approved** | `approved` | `false` | `BRDResponse` | `true` | 🔵 Review (accept) | `*` (reset) | — |

## Design Rules

1. **🟡 Gathering is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic per cycle** — Within each production cycle, the BRD is produced/updated **all at once** with the complete gathered context. Multiple cycles are allowed until the user is satisfied.
3. **Feedback reviews** — If user wants changes at HITL 2, agent asks *"Any more reviews?"* and keeps looping inside 🟡 Gathering until user says no. Then it exits to HITL 1 and applies everything at once.
4. **HITL 1 is mandatory** — The agent cannot auto-jump to 🟢 Production. It must pause, present a summary of gathered requirements, and get explicit user approval. This prevents premature drafting.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from HITL 2 is the only backward transition triggered by the user.

## Draft Lifecycle with Feedback

```
null → HITL1 → BRDResponse (v1) → HITL2 → HITL1 → BRDResponse (v2) → ... → Approved
       ^                         ^
       |                         |
    generate-brd              auto after
    (initial)                 feedback complete
```

**Current behavior:** `current_draft` is overwritten on every re-generation, but `pending_feedback` preserves the set of changes that triggered the update. No version history is kept in state.
