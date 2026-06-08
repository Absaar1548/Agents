# BRD Agent Simplified Flow v2.0 (Chassis-Aligned)

## State-Based Flow Diagram

```mermaid
flowchart TD
    subgraph S1["🟡 Agent Interaction Mode"]
        direction TB
        U1["👤 User Input<br/>Prompt / Feedback (+ optional Document)"]
        ING["📥 Ingestion Pipeline"]
        VDB[("💾 Vector DB")]
        KG[("🕸️ Knowledge Graph<br/>Enterprise KB")]
        GUARD_IN["🛡️ Presidio Input<br/>Mask PII before processing"]
        AGENT["🤖 BRD Agent<br/>(User Prompt + Doc Summary + System Prompt)"]
        ANALYZE["🔍 Analyze & Retrieve Relevant Context<br/>from Vector DB + KG (semantic router)"]
        DEC1{"🤔 Sufficient detail gathered?<br/>(conditional edge)"}
        CLARIFY["❓ Ask Clarifying Questions"]
        HITL1{"👤 HITL 1: interrupt()<br/>Review Summary<br/>(new BRD or planned update)<br/>Proceed / Add More"}

        U1 -->|Document| ING
        ING -->|Processed Document| VDB
        ING -->|Document Summary| AGENT
        U1 -->|User Prompt / Feedback| GUARD_IN
        GUARD_IN -->|Cleaned message| AGENT
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
        GUARD_IN2["🛡️ Presidio Input<br/>Scan context before LLM"]
        GEN["✍️ Produce / Update BRD Document"]
        VAL{"✅ Schema Valid?<br/>(retry loop, max 2)"}
        BRD["📄 BRD Document"]
        GUARD_OUT["🛡️ Presidio Output<br/>Scan BRD for PII"]
        VER["📋 Append to draft_history<br/>(version, provenance)"]

        GUARD_IN2 --> GEN
        GEN --> VAL
        VAL -->|"Valid"| BRD
        VAL -->|"Invalid, retry ≤ 2"| GEN
        VAL -->|"Max retries"| ERR["⚠️ Error Handler"]
        BRD --> GUARD_OUT
        GUARD_OUT --> VER
    end

    subgraph S3["🔵 Review Mode"]
        direction TB
        REVIEW["👀 User Reviews BRD<br/>(interrupt_after)"]
        HITL2{"👤 HITL 2:<br/>Accept or Request Changes?"}
        END["✅ User Accepts<br/>draft_status = APPROVED<br/>— END"]

        REVIEW --> HITL2
        HITL2 -->|"Accept"| END
    end

    %% State Transitions with HITL Gates
    HITL1 -->|"Proceed (interrupt resumes)"| S2
    HITL1 -->|"Add more / Feedback"| U1
    VER -->|"Deliver (HITL 2)"| REVIEW
    HITL2 -->|"Request Changes + optional Document<br/>draft_status = REJECTED<br/>rejection_count += 1"| U1
```

---

## Linear Step-by-Step (with HITL Gates + Guardrails)

```mermaid
flowchart LR
    A["1️⃣ User Input<br/>Prompt / Feedback<br/>(+ optional Document)"] --> B["2️⃣ 🛡️ Presidio<br/>Input Scan"]
    B --> C["3️⃣ Ingestion Pipeline<br/>Store in Vector DB"]
    C --> D["4️⃣ Agent Analysis<br/>Query VDB + KG<br/>(semantic router)"]
    D --> E{"5️⃣ Sufficient detail?<br/>(conditional edge)"}
    E -->|No| F["6️⃣ Ask Clarifying Questions"]
    F --> A
    E -->|Yes| G["7️⃣ 👤 HITL 1: interrupt()<br/>Review Summary<br/>Proceed / Add More"]
    G -->|Add more| A
    G -->|Proceed| H["8️⃣ 🛡️ Scan + Produce BRD<br/>(retry loop, max 2)"]
    H --> I["9️⃣ 📋 Version + Deliver BRD"]
    I --> J["🔟 👤 HITL 2: interrupt_after<br/>User Reviews BRD"]
    J --> K{"1️⃣1️⃣ Accept or<br/>Request Changes?"}
    K -->|Accept| Z["🔚 END ✅<br/>draft_status = APPROVED"]
    K -->|"Changes<br/>rejection_count += 1"| A
```

---

## State Machine Table

| State | Purpose | Entry From | Exit To | HITL Gate |
|-------|---------|------------|---------|-----------|
| 🟡 **Agent Interaction** | Ingest & summarize document, analyze, retrieve from Vector DB + enterprise KG (semantic router), ask clarifying questions, collect review feedback. Presidio scans every message. Agent decides when enough detail is gathered. | User input / HITL1 (Add more) / Review changes | 🟢 BRD Production | **HITL 1** — `interrupt()` in `hitl_gate` node |
| 🟢 **BRD Production** | Generate initial BRD **or** update existing BRD atomically with complete gathered context. Validation retry loop (max 2). Presidio input/output guardrails. Draft appended to `draft_history`. | 🟡 Agent Interaction (HITL 1 approved) | 🔵 Review Mode / ⚠️ Error | Auto-transition via `interrupt_after` |
| 🔵 **Review Mode** | User reviews delivered BRD and decides to accept or request changes. `draft_status` tracked in state (not session flag). `rejection_count` incremented on changes. | 🟢 BRD Production | ✅ END (accept) / 🟡 Agent Interaction (changes) | **HITL 2** — `interrupt_after` on `schema_validate` |

---

## The Two HITL Gates (LangGraph Interrupts)

| Gate | Implementation | Location | Prompt | Outcome A | Outcome B |
|------|---------------|----------|--------|-----------|-----------| 
| **HITL 1** | `interrupt()` in `hitl_gate` node | 🟡 → 🟢 | *New BRD:* "Here's a summary of what I've gathered — add anything, or proceed?" · *Update:* "Based on your feedback, here's the update I'll make — proceed, or add more?" | `Proceed` → 🟢 BRD Production | `Add more` → stay in 🟡 |
| **HITL 2** | `interrupt_after=["schema_validate"]` | Inside 🔵 | *"Please review the BRD. Accept, or request changes?"* | `Accept` → ✅ END | `Request changes` → 🟡 (rejection_count += 1) |

---

## Key Design Rules

1. **🟡 Agent Interaction is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic** — BRD is produced/updated **once** per cycle with the complete gathered context. Multiple cycles are allowed.
3. **Feedback re-entry is not a separate mode** — it is the same Agent Interaction loop with the current draft in context. Ends at the same HITL 1 gate.
4. **HITL gates are LangGraph interrupts** (Chassis §3.2.2) — no ad-hoc branches. Graph pauses at SqliteSaver level.
5. **🟢 → 🔵 is auto** — Once production completes, it auto-delivers to Review Mode.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes is the only backward transition.
7. **KG is pre-seeded enterprise KB** — KG/VDB are direct retriever calls, not MCP (Chassis §3.3.1).
8. **Presidio guardrails wrap every LLM call** (Chassis §3.5) — mask financial/contact PII; log person names.
9. **Every production cycle is versioned** — `draft_history` is append-only with full provenance.
10. **Rejection count tracked, no hard cap** — Production env will enforce Chassis §3.8 (3 rejections).