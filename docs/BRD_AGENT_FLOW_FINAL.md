# BRD Agent Flow Diagram (Final — 3-State + HITL Gates)

## State-Based Flow Diagram

```mermaid
flowchart TD
    subgraph S1["🟡 Information Gathering Mode"]
        direction TB
        U1["👤 User Input: Prompt + Document"]
        ING["📥 Ingestion Pipeline"]
        VDB[(💾 Vector DB)]
        KG[(🕸️ Knowledge Graph)]
        AGENT["🤖 BRD Agent<br/>(Prompt + Summary + System Prompt)"]
        ANALYZE["🔍 Analyze & Retrieve<br/>from Vector DB + KG"]
        DEC1{"🤔 Enough Info?"}
        CLARIFY["❓ Ask Clarifying Questions / Any More Reviews"]
        HITL1{"👤 HITL 1:<br/>Review Summary &<br/>Proceed to Production?"}

        U1 --> ING
        ING -->|Processed Document| VDB
        ING -->|Document Summary| AGENT
        U1 -->|User Prompt| AGENT
        AGENT --> ANALYZE
        ANALYZE -->|Query| VDB
        ANALYZE -->|Query| KG
        ANALYZE --> DEC1
        DEC1 -->|No| CLARIFY
        CLARIFY -->|Answer| U1
    end

    subgraph S2["🟢 BRD Production Mode<br/>Generate OR Update"]
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
        HITL2 -->|No / Accept| END
    end

    %% State Transitions with HITL Gates
    DEC1 -->|Yes| HITL1
    HITL1 -->|Yes — Proceed| S2
    HITL1 -->|No — Needs More Info| U1
    BRD -->|Deliver| REVIEW
    HITL2 -->|Yes — Changes Needed| S1
```

---

## Linear Step-by-Step (with HITL Gates)

```mermaid
flowchart LR
    A["1️⃣ User Input<br/>Prompt + Document"] --> B["2️⃣ Ingestion Pipeline"]
    B --> C["3️⃣ Store in Vector DB"]
    C --> D["4️⃣ Agent Analysis<br/>Query VDB + KG"]
    D --> E{"5️⃣ Enough Info?"}
    E -->|No| F["6️⃣ Ask Clarifying Questions<br/>or Any More Reviews?"]
    F --> A
    E -->|Yes| G["6️⃣🅰️ HITL 1:<br/>👤 Review Summarized Info<br/>& Confirm Production"]
    G -->|No — Needs More| A
    G -->|Yes — Proceed| H["7️⃣ Produce BRD Document<br/>(Generate or Update)"]
    H --> I["8️⃣ Deliver BRD to User"]
    I --> J["9️⃣ 👤 HITL 2:<br/>User Reviews BRD"]
    J --> K{"🔟 Accept or Changes?"}
    K -->|Accept| Z["🔚 END ✅"]
    K -->|Changes Needed| A
```

---

## State Machine Table

| State | Purpose | Entry From | Exit To | HITL Gate |
|-------|---------|------------|---------|-----------|
| 🟡 **Information Gathering** | Ingest, summarize, analyze, retrieve from VDB+KG, ask clarifying questions, collect **all** review feedback. Agent decides when enough info is gathered. | User input / Review changes | 🟢 BRD Production | **HITL 1** — User must review summary & confirm before production |
| 🟢 **BRD Production** | Generate initial BRD **or** update existing BRD atomically with all gathered context | 🟡 Gathering (HITL 1 = Yes) | 🔵 Review Mode | Auto-transition on completion |
| 🔵 **Review Mode** | User reviews delivered BRD. Decides to accept or request changes. | 🟢 BRD Production | ✅ END (HITL 2 = Accept) / 🟡 Gathering (HITL 2 = Changes) | **HITL 2** — User decides accept or changes |

---

## The Two HITL Gates

| Gate | Location | Question | Yes → | No → |
|------|----------|----------|-------|------|
| **HITL 1** | Between 🟡 Gathering and 🟢 Production | *"I've gathered enough info. Here's a summary. Shall I proceed to generate/update the BRD?"* | 🟢 BRD Production | 🟡 Information Gathering (agent asks more questions) |
| **HITL 2** | Inside 🔵 Review Mode | *"Please review the BRD. Accept or request changes?"* | ✅ END | 🟡 Information Gathering (batch feedback collection) |

---

## Key Design Rules

1. **🟡 Gathering is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic** — BRD is produced/updated **once** per cycle with the complete gathered context.
3. **Batch reviews** — If user wants changes at HITL 2, agent asks *"Any more reviews?"* and keeps looping inside 🟡 Gathering until user says no. Then it exits to 🟢 Production and applies everything at once.
4. **HITL 1 is mandatory** — The agent cannot auto-jump to 🟢 Production. It must pause, present a summary of gathered requirements, and get explicit user approval. This prevents premature drafting.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from Review Mode is the only backward transition triggered by the user.
