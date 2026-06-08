# Drafting Graph Sequence Diagram v2.0 (Validation Retry Loop)

This diagram shows the exact step-by-step execution inside the **Drafting Graph**, which now produces or updates the BRD **atomically** with a **validation retry loop** (max 2 retries), **Presidio guardrails**, and **draft versioning**.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant E as /generate-brd Endpoint
    participant S as ChatbotState (SqliteSaver)
    participant G as LangGraph Thread
    participant N1 as retrieve_context
    participant GI as 🛡️ Presidio Input
    participant N2 as draft_llm
    participant N3 as schema_validate
    participant GO as 🛡️ Presidio Output
    participant R as route_after_validation
    participant N4 as error_handler
    participant LLM as LLM Client
    participant CH as Chroma
    participant NEO as Neo4j

    U->>E: POST /generate-brd {session_id}
    Note over E: Triggered by:<br/>1. HITL 1 approval — user confirms via interrupt resume<br/>2. User manually clicks "Generate BRD"

    E->>S: set mode = "drafting", retry_count = 0
    E->>G: invoke with existing ChatbotState (thread_id = session_id)

    rect rgb(240, 230, 255)
        Note over G,N1: Node 1: retrieve_context (RetryPolicy: 2)
        G->>N1: pass state
        N1->>S: read mode, messages, brd_memory,<br/>rolling_summary, last_retrievals, pending_feedback,<br/>validation_errors
        N1->>N1: infer_strategy()
        alt current_draft exists AND pending_feedback not empty
            N1->>N1: strategy = BRD_UPDATE
            Note over N1: Use UPDATE_SYSTEM_PROMPT<br/>"Update the existing BRD using all feedback..."
        else
            N1->>N1: strategy = BRD_GENERATION
            Note over N1: Use DRAFTING_SYSTEM_PROMPT<br/>"Generate a complete BRD..."
        end
        alt retry_count > 0
            Note over N1: RETRY: Inject validation_errors<br/>into prompt for LLM self-correction
        end
        N1->>CH: query (top 4 hits)
        CH-->>N1: document chunks
        N1->>NEO: entity linking + 1-hop (top 4 entities)
        NEO-->>N1: entities + neighbours
        N1->>N1: ContextAssembler.assemble()
        Note over N1: Layers:<br/>• System prompt (generation OR update variant)<br/>• Full BRDMemory block<br/>• Rolling summary<br/>• All conversation turns<br/>• Doc/KG hits (heavy retrieval)<br/>• [if BRD_UPDATE] current_draft + ALL pending_feedback<br/>• [if retry] validation_errors from previous attempt<br/>• JSON schema instruction
        N1->>S: write last_retrievals["assembled"]
        N1-->>G: return state updates
    end

    rect rgb(255, 245, 230)
        Note over G,N2: Node 2: draft_llm (RetryPolicy: 3)
        G->>N2: pass state
        N2->>S: read last_retrievals["assembled"]["messages"]
        N2->>GI: Scan assembled context for PII
        GI-->>N2: cleaned context + findings log
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
            N3->>GO: Scan validated BRD for PII
            GO-->>N3: cleaned BRD + findings log
            N3->>S: write current_draft = validated dict
            N3->>S: draft_status = DRAFT
            N3->>S: clear pending_feedback = []
            N3->>S: clear validation_errors = []
            N3->>S: append to draft_history (version, provenance)
            N3->>S: mark span OK
        else Parse error OR Validation error
            N3->>S: retry_count += 1
            N3->>S: validation_errors = [error details]
            N3->>S: mark span WARNING
        end
        N3-->>G: return state updates
    end

    rect rgb(255, 255, 220)
        Note over G,R: Conditional Edge: route_after_validation
        G->>R: inspect state
        alt current_draft is valid (success)
            R-->>G: route → END
        else invalid AND retry_count < 2
            R-->>G: route → draft_llm (RETRY)
            Note over R: validation_errors injected<br/>into next retrieve_context call
        else invalid AND retry_count >= 2
            R-->>G: route → error_handler
        end
    end

    alt Route: END (success)
        G-->>E: final ChatbotState
        E->>E: interrupt_after triggers HITL 2
        E->>E: auto-transition to Review Mode
        Note over E: User must review BRD and decide:<br/>Accept → END / Changes → feedback gathering
        E-->>U: {draft: BRDResponse, mode: awaiting_approval, version: N, session_id}
    else Route: RETRY (loop back to draft_llm)
        Note over G: Graph loops: retrieve_context → draft_llm → schema_validate
        Note over G: validation_errors are included in next prompt
    else Route: error_handler (max retries exceeded)
        rect rgb(255, 200, 200)
            Note over G,N4: error_handler — graceful degradation
            G->>N4: pass state
            N4->>S: read retry_count, validation_errors
            N4->>S: write reply_text = "Unable to produce valid BRD after 2 retries"
            Note over N4: pending_feedback preserved for user retry
            N4-->>G: return state updates
        end
        G-->>E: final ChatbotState
        E-->>U: {error: "validation_failed", validation_errors, retry_count, session_id}
    end
```

## Node Execution Summary

| Step | Node | Reads From State | Writes To State | External I/O | Retry |
|---|---|---|---|---|---|
| 1 | `retrieve_context` | `mode`, `messages`, `brd_memory`, `rolling_summary`, `last_retrievals`, `pending_feedback`, `validation_errors` | `last_retrievals["assembled"]` | Chroma (4 hits), Neo4j (4 entities) | RetryPolicy(2) |
| 2 | `draft_llm` | `last_retrievals["assembled"]` | `last_retrievals["draft_raw"]` | Presidio (input) + LLM (JSON mode, 4k tokens) | RetryPolicy(3) |
| 3 | `schema_validate` | `last_retrievals["draft_raw"]` | `current_draft`, `draft_status`, `draft_history`, `retry_count`, `validation_errors`, clears `pending_feedback` | Pydantic validation + Presidio (output) | — |
| — | `route_after_validation` | `current_draft`, `retry_count` | — | — (pure routing) | — |
| 4 | `error_handler` | `retry_count`, `validation_errors` | `reply_text` | — | — |

## v2 Key Changes

### 1. Validation Retry Loop
- **v1:** Validation failure → exception → graph halts → 500 error to user.
- **v2:** Validation failure → `retry_count += 1` → `validation_errors` stored → `route_after_validation` routes back to `draft_llm` (max 2 retries). On each retry, the validation errors are injected into the prompt so the LLM can self-correct (Chassis §3.5: pre-HITL self-check).

### 2. Presidio Guardrails
- **Input:** Assembled context is scanned for PII before sending to LLM.
- **Output:** Validated BRD is scanned for PII leakage before returning to user.
- **Entity scope:** Mask SSN, credit cards, bank numbers, email, phone. Log-only: person names, IPs.

### 3. Draft Versioning
Each successful `schema_validate` appends an entry to `draft_history`:
```python
{
    "version": N,
    "draft": brd.model_dump(mode="json"),
    "produced_at": "2026-06-08T12:00:00Z",
    "prompt_hash": "sha256:abc123...",
    "context_strategy": "BRD_GENERATION",
    "model": "gpt-4o",
    "token_accounting": {...},
    "status": "draft",  # → approved / rejected
    "reviewed_by": None,
    "reviewed_at": None,
}
```

### 4. Pending Feedback Cleared on Success Only
- **v1:** Not applicable (no feedback gathering).
- **v2:** `schema_validate` clears `pending_feedback = []` only after successful validation. If validation fails, the feedback remains in state for the retry loop.

### 5. Error Handling (v2 Flow)

```
draft_llm → schema_validate
                    │
         route_after_validation
        ┌───────────┼───────────┐
        │           │           │
       END      draft_llm   error_handler
    (success)   (retry ≤2)   (max retries)
        │           │           │
    HITL 2      inject        return
  (interrupt)   errors     {error, details}
```
