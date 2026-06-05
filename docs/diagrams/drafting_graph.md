# Drafting Graph Sequence Diagram (Atomic Production)

This diagram shows the exact step-by-step execution inside the **Drafting Graph**, which now produces or updates the BRD **atomically** with complete gathered context.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant E as /generate-brd Endpoint
    participant S as ChatbotState
    participant G as LangGraph Thread
    participant N1 as retrieve_context
    participant N2 as draft_llm
    participant N3 as schema_validate
    participant LLM as LLM Client
    participant CH as Chroma
    participant NEO as Neo4j

    U->>E: POST /generate-brd
    Note over E: Triggered by:<br/>1. HITL 1 approval — user confirms after reviewing summary<br/>2. User manually clicks "Generate BRD" (legacy path)

    E->>S: set mode = "drafting"
    E->>G: invoke with existing ChatbotState

    rect rgb(240, 230, 255)
        Note over G,N1: Node 1: retrieve_context (Atomic Production)
        G->>N1: pass state
        N1->>S: read mode, messages, brd_memory,<br/>rolling_summary, last_retrievals, pending_feedback
        N1->>N1: infer_strategy()
        alt current_draft exists AND pending_feedback not empty
            N1->>N1: strategy = BRD_UPDATE
            Note over N1: Use UPDATE_SYSTEM_PROMPT<br/>"Update the existing BRD using all feedback..."
        else
            N1->>N1: strategy = BRD_GENERATION
            Note over N1: Use DRAFTING_SYSTEM_PROMPT<br/>"Generate a complete BRD..."
        end
        N1->>CH: query (top 4 hits)
        CH-->>N1: document chunks
        N1->>NEO: entity linking + 1-hop (top 4 entities)
        NEO-->>N1: entities + neighbours
        N1->>N1: ContextAssembler.assemble()
        Note over N1: Layers:<br/>• System prompt (generation OR update variant)<br/>• Full BRDMemory block<br/>• Rolling summary<br/>• All conversation turns<br/>• Doc/KG hits (heavy retrieval)<br/>• [if BRD_UPDATE] current_draft + ALL pending_feedback<br/>• JSON schema instruction
        N1->>S: write last_retrievals["assembled"]
        N1-->>G: return state updates
    end

    rect rgb(255, 245, 230)
        Note over G,N2: Node 2: draft_llm
        G->>N2: pass state
        N2->>S: read last_retrievals["assembled"]["messages"]
        N2->>LLM: chat completion<br/>response_format={"type":"json_object"}<br/>temp=0.2, max_tokens=4000
        LLM-->>N2: raw JSON string (may have markdown fences)
        N2->>S: write last_retrievals["draft_raw"]
        N2-->>G: return state updates
    end

    rect rgb(255, 230, 230)
        Note over G,N3: Node 3: schema_validate
        G->>N3: pass state
        N3->>S: read last_retrievals["draft_raw"]
        N3->>N3: extract JSON from markdown fences / wrappers
        N3->>N3: inject drafted_by = "brd-agent@0.1.0"
        alt JSON parse succeeds AND Pydantic validation passes
            N3->>S: write current_draft = validated dict
            N3->>S: clear pending_feedback = []
            N3->>S: mark span OK
            N3-->>G: return state updates
        else Parse error OR Validation error
            N3->>N3: mark span ERROR
            N3--xG: RAISE exception
            G--xE: Graph halts
            E-->>U: 500 error response
        end
    end

    G-->>E: final ChatbotState
    E->>E: build response
    E->>E: auto-transition to Review Mode (HITL 2)
    Note over E: User must review BRD and decide:<br/>Accept → END / Changes → feedback gathering
    E-->>U: {draft: BRDResponse, mode: awaiting_approval}
```

## Node Execution Summary

| Step | Node | Reads From State | Writes To State | External I/O |
|---|---|---|---|---|
| 1 | `retrieve_context` | `mode`, `messages`, `brd_memory`, `rolling_summary`, `last_retrievals`, `pending_feedback` | `last_retrievals["assembled"]` | Chroma (4 hits), Neo4j (4 entities) |
| 2 | `draft_llm` | `last_retrievals["assembled"]` | `last_retrievals["draft_raw"]` | LLM (JSON mode, 4k tokens) |
| 3 | `schema_validate` | `last_retrievals["draft_raw"]` | `current_draft`, clears `pending_feedback` | Pydantic validation |

## Key Changes from Previous Version

### 1. Atomic Production
- **Before:** Drafting was only for initial generation. Revisions were ad-hoc through `/request-changes`.
- **Now:** Drafting handles both **initial generation** (`BRD_GENERATION`) and **atomic updates** (`BRD_UPDATE`). In both cases, the BRD is produced **once** with complete context.

### 2. Pending Feedback Inclusion
- **Before:** `pending_feedback` did not exist. Each `/request-changes` was a single feedback item that immediately triggered discussion.
- **Now:** `pending_feedback` is read by `retrieve_context` during `BRD_UPDATE` and injected into the prompt as a structured list. The LLM sees all accumulated changes at once.

### 3. Prompt Variants
| Variant | When Used | Key Instruction |
|---|---|---|
| `DRAFTING_SYSTEM_PROMPT` | `BRD_GENERATION` | "Generate a complete BRD from scratch matching this JSON schema..." |
| `UPDATE_SYSTEM_PROMPT` | `BRD_UPDATE` | "Update the existing BRD using the feedback items below. Preserve unchanged sections. Output full updated JSON..." |

### 4. Pending Feedback Cleared on Success
- **Before:** Not applicable (no feedback gathering).
- **Now:** `schema_validate` clears `pending_feedback = []` only after successful validation. If validation fails, the feedback remains in state so the retry (when implemented) can re-use it.

## Error Handling in Drafting Graph

```
┌─────────────────────────────────────────────────────────────┐
│  draft_llm → schema_validate                                │
│                                                             │
│  ┌─────────────┐    ┌─────────────────┐    ┌────────────┐  │
│  │  Raw JSON   │──>│  JSON.parse()   │──>│ Pydantic   │  │
│  │  (string)   │    │  (strip fences) │    │ validate   │  │
│  └─────────────┘    └─────────────────┘    └────────────┘  │
│                                                         │    │
│                                                    [FAIL]    │
│                                                         │    │
│                                                         ▼    │
│                                                  Exception   │
│                                                  Graph HALT  │
│                                                  500 to user │
│                                                  pending_feedback KEPT │
│                                                         │    │
│                                                    [PASS]    │
│                                                         ▼    │
│                                                  current_draft│
│                                                  pending_feedback CLEARED│
│                                                  awaiting_approval│
└─────────────────────────────────────────────────────────────┘
```

**Current gap:** No retry loop. If validation fails, the entire request fails. Because `pending_feedback` is **not cleared on failure**, a future retry mechanism can re-use the accumulated feedback.
