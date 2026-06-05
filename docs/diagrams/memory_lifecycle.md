# Memory Lifecycle Diagram (Simplified 3-State)

This diagram shows how facts flow from conversation → LangMem extraction → merged memory → prompt context across multiple turns, now updated for **feedback gatherings** and **atomic production**.

```mermaid
flowchart LR
    subgraph T1["Turn N — Initial Gathering"]
        U1["User: 'We need SSO for 500 users'"]
        A1["Assistant: 'Noted. What identity provider?'"]
        E1["extract_memory node"]
        M1["LangMem extracts:<br/>• constraint: SSO required<br/>• metric: 500 users"]
    end

    subgraph T2["Turn N+1 — Clarification"]
        U2["User: 'Azure AD. Also 99.9% uptime'"]
        A2["Assistant: 'Got it. Any compliance needs?'"]
        E2["extract_memory node"]
        M2["LangMem extracts:<br/>• stakeholder: Azure AD<br/>• nfr: 99.9% uptime<br/>• merges with prior memory"]
    end

    subgraph T3["Turn N+2 — Feedback Gathering"]
        U3["User: 'Change priority to High. Add audit log requirement.'"]
        A3["Assistant: 'Any more reviews?'"]
        E3["extract_memory node"]
        M3["LangMem extracts:<br/>• fr_update: priority = High<br/>• new_fr: audit log requirement"]
    end

    subgraph T4["Turn N+3 — More Feedback"]
        U4["User: 'Also add GDPR clause.'"]
        A4["Assistant: 'Any more reviews?'"]
        E4["extract_memory node"]
        M4["LangMem extracts:<br/>• constraint: GDPR clause<br/>User says: 'No more reviews'<br/>→ feedback_gathering cleared"]
    end

    subgraph STORE["🧠 LangMem Store<br/>namespace: brd_agent/&lt;session_id&gt;"]
        direction TB
        MEM["BRDMemory items"]
        SUM["ConversationSummary items"]
    end

    subgraph FEEDBACK["📝 Pending Feedback Buffer"]
        PF["pending_feedback: list[str]"]
    end

    subgraph PROMPT["📋 Prompt Injection"]
        P1["retrieve_context reads<br/>brd_memory from state"]
        P2["BRDMemory block injected<br/>into assembled context"]
        P3["[BRD_UPDATE] pending_feedback<br/>items 1..N injected"]
    end

    U1 --> A1
    A1 --> E1
    E1 -->|"last 2 msgs"| M1
    M1 -->|"store"| MEM
    MEM -->|"read back merged"| E1
    E1 -->|"write"| S1["state.brd_memory"]

    U2 --> A2
    A2 --> E2
    E2 -->|"last 2 msgs + existing memory"| M2
    M2 -->|"store"| MEM
    MEM -->|"read back merged"| E2
    E2 -->|"write"| S1

    U3 --> A3
    A3 --> E3
    E3 -->|"last 2 msgs + existing memory"| M3
    M3 -->|"store"| MEM
    MEM -->|"read back merged"| E3
    E3 -->|"write"| S1
    E3 -->|"append"| PF

    U4 --> A4
    A4 --> E4
    E4 -->|"last 2 msgs + existing memory"| M4
    M4 -->|"store"| MEM
    MEM -->|"read back merged"| E4
    E4 -->|"write"| S1
    E4 -->|"append"| PF

    S1 -->|"read"| P1
    P1 --> P2
    PF -->|"[BRD_UPDATE only]"| P3
    P2 -->|"feeds into"| LLM["LLM Prompt"]
    P3 -->|"feeds into"| LLM

    %% Summarize branch
    SUM -.->|"written by summarize node<br/>when messages > 12"| S2["state.rolling_summary"]
    S2 -.->|"read by retrieve_context"| P1
```

## Dual Memory Tracks

The system maintains two parallel memory systems in the same LangMem namespace, discriminated by schema:

| Track | Schema | Populated By | Used By | Lifecycle |
|---|---|---|---|---|
| **BRDMemory** | Structured fields (title, objectives, requirements, etc.) | `extract_memory` node | `retrieve_context` (prompt block) | Accumulates across all turns; updated by both initial chat and feedback gathering feedback. |
| **ConversationSummary** | `recent_topics`, `decisions_so_far`, `pending_clarifications`, `compressed_narrative` | `summarize` node | `retrieve_context` (summary block) | Replaced/merged each time transcript exceeds threshold. |

## NEW: Pending Feedback Buffer

**`pending_feedback`** is **not** part of LangMem. It is a simple list in `ChatbotState` that accumulates review items:

```python
pending_feedback = [
    "Change priority of FR-003 to High",
    "Add audit log requirement for all admin actions",
    "Include GDPR compliance clause in assumptions"
]
```

**Lifecycle:**
1. User sends feedback via `/request-changes` → appended to `pending_feedback`.
2. Agent asks "Any more reviews?" → loop continues.
3. User says "no more reviews" → `feedback_gathering = false`, auto-transition to `/generate-brd`.
4. During `BRD_UPDATE`, `retrieve_context` injects **all** `pending_feedback` items into the prompt.
5. After successful `schema_validate`, `pending_feedback` is **cleared**.
6. If validation fails, `pending_feedback` **remains** in state for retry.

## Current Limitations

1. **No versioning within BRDMemory:** If a requirement is revised, the old version is overwritten in LangMem. There is no audit trail of what changed.
2. **Extraction is turn-local:** `extract_memory` only looks at the last 2 messages. If a fact is implied across 3+ messages, it may be missed or only partially captured.
3. **No negative memory:** There is no mechanism to explicitly "remove" a fact. A user saying "Actually, forget the SSO requirement" may result in both the old and new facts coexisting in memory.
4. **Drafting graph does not extract memory:** After `/generate-brd`, no new `BRDMemory` is written even if the draft reveals gaps or contradictions.
5. **Memory is session-scoped:** `brd_agent/<session_id>` means each `/reset` starts with a blank memory. There is no cross-session learning or template memory inheritance.
6. **Pending feedback is plain text:** `pending_feedback` is a list of raw strings. The LLM must interpret them semantically during `BRD_UPDATE`. There is no structured parsing of feedback into actionable field mutations.
