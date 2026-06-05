# BRD Agent Flow Diagram (Final — 3-State + HITL Gates)

## State-Based Flow Diagram

```mermaid
flowchart TD
    subgraph S1["🟡 Agent Interaction Mode"]
        direction TB
        U1["👤 User Input<br/>Prompt / Feedback (+ optional Document)"]
        ING["📥 Ingestion Pipeline"]
        VDB[(💾 Vector DB)]
        KG[(🕸️ Knowledge Graph<br/>Enterprise KB)]
        AGENT["🤖 BRD Agent<br/>(User Prompt + Doc Summary + System Prompt)"]
        ANALYZE["🔍 Analyze & Retrieve Relevant Context<br/>from Vector DB + KG"]
        DEC1{"🤔 Sufficient detail gathered?"}
        CLARIFY["❓ Ask Clarifying Questions"]
        HITL1{"👤 HITL 1: Review Summary<br/>(new BRD or planned update)<br/>Add more or proceed?"}

        U1 -->|Document| ING
        ING -->|Processed Document| VDB
        ING -->|Document Summary| AGENT
        U1 -->|User Prompt / Feedback| AGENT
        AGENT --> ANALYZE
        ANALYZE -->|Query| VDB
        ANALYZE -->|Query| KG
        ANALYZE --> DEC1
        DEC1 -->|No| CLARIFY
        CLARIFY -->|Answer| U1
        DEC1 -->|Yes| HITL1
    end

    subgraph S2["🟢 BRD Production Mode<br/>Generate OR Update (atomic)"]
        direction TB
        GEN["✍️ Produce / Update BRD Document"]
        BRD["📄 BRD Document"]
        GEN --> BRD
    end

    subgraph S3["🔵 Review Mode"]
        direction TB
        REVIEW["👀 User Reviews BRD"]
        HITL2{"👤 HITL 2:<br/>Accept or Request Changes?"}
        END["✅ User Accepts — END"]

        REVIEW --> HITL2
        HITL2 -->|Accept| END
    end

    %% State Transitions with HITL Gates
    HITL1 -->|Proceed| S2
    HITL1 -->|Add more / Feedback| U1
    BRD -->|Deliver| REVIEW
    HITL2 -->|Request Changes + optional Document| U1
```

---

## Linear Step-by-Step (with HITL Gates)

```mermaid
flowchart LR
    A["1️⃣ User Input<br/>Prompt / Feedback (+ optional Document)"] --> B["2️⃣ Ingestion Pipeline"]
    B --> C["3️⃣ Store in Vector DB"]
    C --> D["4️⃣ Agent Analysis<br/>Query Vector DB + Enterprise KG"]
    D --> E{"5️⃣ Sufficient detail gathered?"}
    E -->|No| F["6️⃣ Ask Clarifying Questions"]
    F --> A
    E -->|Yes| G["7️⃣ 👤 HITL 1: Review Summary<br/>(new BRD or planned update)<br/>Add more or proceed?"]
    G -->|Add more / Feedback| A
    G -->|Proceed| H["8️⃣ Produce / Update BRD<br/>(atomic, one cycle)"]
    H --> I["9️⃣ Deliver BRD to User"]
    I --> J["🔟 👤 HITL 2: User Reviews BRD"]
    J --> K{"1️⃣1️⃣ Accept or Request Changes?"}
    K -->|Accept| Z["🔚 END ✅"]
    K -->|Request Changes| A
```

---

## State Machine Table

| State | Purpose | Entry From | Exit To | HITL Gate |
|-------|---------|------------|---------|-----------|
| 🟡 **Agent Interaction** | Ingest & summarize document, analyze, retrieve from Vector DB + enterprise KG, ask clarifying questions, and collect review feedback. Agent decides when enough detail is gathered. | User input / Review changes | 🟢 BRD Production | **HITL 1** — user reviews the summary, then adds more or approves production |
| 🟢 **BRD Production** | Generate initial BRD **or** update existing BRD atomically, all at once, with the complete gathered context. Multiple cycles allowed. | 🟡 Agent Interaction (HITL 1 approved) | 🔵 Review Mode | Auto-transition on completion |
| 🔵 **Review Mode** | User reviews delivered BRD and decides to accept or request changes. | 🟢 BRD Production | ✅ END (accept) / 🟡 Agent Interaction (request changes) | **HITL 2** — user accepts or requests changes |

---

## The Two HITL Gates

| Gate | Location | Prompt | Outcome A | Outcome B |
|------|----------|--------|-----------|-----------|
| **HITL 1** | 🟡 Agent Interaction → 🟢 Production | *New BRD:* "Here's a summary of what I've gathered — add anything, or proceed?" · *Update:* "Based on your feedback, here's the update I'll make to the current doc — proceed, or add more feedback?" | `Proceed` → 🟢 BRD Production | `Add more / feedback` → stay in 🟡 Agent Interaction |
| **HITL 2** | Inside 🔵 Review Mode | *"Please review the BRD. Accept, or request changes?"* | `Accept` → ✅ END | `Request changes (+ optional document)` → 🟡 Agent Interaction |

---

## Key Design Rules

1. **🟡 Agent Interaction is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic** — BRD is produced/updated **once** per cycle with the complete gathered context. Multiple cycles are allowed (initial generation, then updates after feedback).
3. **Feedback re-entry is not a separate mode** — it is the same Agent Interaction loop, entered with the current draft in context. The user may attach supporting documents with feedback (ingested through the same pipeline), and the loop ends at the same HITL 1 gate.
4. **HITL 1 is the single per-cycle approval** — for both generation and updates. The agent cannot auto-jump to 🟢 Production; it must pause and present a summary (gathered requirements for a new BRD, or the concrete planned change to the current document for an update) and get explicit approval. The user either proceeds or adds more feedback. This prevents premature drafting.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from Review Mode (HITL 2) is the only backward transition triggered by the user.
7. **The Knowledge Graph is a pre-seeded enterprise KB** — Ingestion populates the Vector DB only; the agent reads enterprise context (stakeholders, business units, regulatory domains, systems) from the KG but does not write to it. Enriching the KG from ingested documents is a future option, not part of the current flow.