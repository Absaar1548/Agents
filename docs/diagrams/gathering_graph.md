# Agent Interaction Graph Sequence Diagram v2.0 (Conditional Edges + HITL Interrupt)

This diagram shows the exact step-by-step execution inside the **Agent Interaction Graph**, which is the **universal entry point** for all user input. In v2, the graph uses **conditional edges** after `invoke_llm` and a dedicated **`hitl_gate` node** with LangGraph `interrupt()`.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant E as /chat or /request-changes Endpoint
    participant GI as 🛡️ Presidio Input
    participant S as ChatbotState (SqliteSaver)
    participant G as LangGraph Thread
    participant N1 as summarize
    participant N2 as retrieve_context
    participant N3 as invoke_llm
    participant GO as 🛡️ Presidio Output
    participant R as route_after_conversation
    participant N4a as hitl_gate
    participant N4b as extract_memory
    participant LLM as LLM Client
    participant CH as Chroma
    participant NEO as Neo4j
    participant ART as ArtifactStore
    participant LM as LangMem Store (InMemory)

    U->>E: POST /chat {message, session_id}
    Note over E: OR POST /request-changes {feedback, session_id}

    E->>E: Mode decision
    alt initial chat
        E->>S: mode = gathering
    else review re-entry
        E->>S: mode = request_changes
        E->>S: feedback_gathering = true
        E->>S: draft_status = REJECTED, rejection_count += 1
        E->>S: append feedback to pending_feedback
    end
    E->>GI: Scan user message for PII
    GI-->>E: cleaned message + findings log
    E->>S: append HumanMessage to messages
    E->>G: invoke with ChatbotState (thread_id = session_id)

    rect rgb(230, 245, 255)
        Note over G,N1: Node 1: summarize (RetryPolicy: 3)
        G->>N1: pass state
        N1->>S: read messages
        alt len(messages) > 12
            N1->>LLM: SUMMARIZE_SYSTEM_PROMPT + oldest slice
            LLM-->>N1: ConversationSummary JSON
            N1->>S: merge into rolling_summary
            N1->>S: emit RemoveMessage (drop old turns)
        else len(messages) ≤ 12
            Note over N1: No-op
        end
        N1-->>G: return state updates
    end

    rect rgb(240, 230, 255)
        Note over G,N2: Node 2: retrieve_context (RetryPolicy: 2)
        G->>N2: pass state
        N2->>S: read mode, messages, brd_memory,<br/>rolling_summary, current_draft, last_retrievals,<br/>pending_feedback, feedback_gathering
        N2->>N2: infer ContextStrategy (semantic router)
        alt feedback_gathering == true
            N2->>N2: strategy = REFINEMENT (or BRD_UPDATE)
        else mode == request_changes
            N2->>N2: strategy = REFINEMENT
        else semantic router match
            N2->>N2: strategy = CLARIFICATION / COMPLIANCE_HEAVY / etc.
        else
            N2->>N2: strategy = INFORMATION_GATHERING
        end
        N2->>CH: query (top-k by strategy)
        CH-->>N2: document hits
        N2->>NEO: entity linking + 1-hop traversal
        NEO-->>N2: entity/neighbour hits
        N2->>ART: read artifact_refs
        ART-->>N2: artifact summaries
        N2->>N2: ContextAssembler.assemble()<br/>→ build prompt stack + token accounting
        Note over N2: If REFINEMENT/BRD_UPDATE:<br/>Inject current_draft + ALL pending_feedback
        N2->>S: write last_retrievals["assembled"]
        N2-->>G: return state updates
    end

    rect rgb(255, 245, 230)
        Note over G,N3: Node 3: invoke_llm (RetryPolicy: 3)
        G->>N3: pass state
        N3->>S: read last_retrievals["assembled"]["messages"]
        N3->>LLM: chat completion (temp=0.4, max_tokens=800)
        LLM-->>N3: AIMessage content
        N3->>GO: Scan LLM reply for PII
        GO-->>N3: cleaned reply + findings log
        alt enough_info == true
            N3->>S: ready_for_production = true
        else feedback_gathering AND user says "no more reviews"
            N3->>S: feedback_gathering = false
            N3->>S: ready_for_production = true
        else
            Note over N3: Continue gathering / feedback collection
        end
        N3->>S: append AIMessage to messages
        N3->>S: write reply_text
        N3-->>G: return state updates
    end

    rect rgb(255, 255, 220)
        Note over G,R: Conditional Edge: route_after_conversation
        G->>R: inspect state
        alt ready_for_production == true
            R-->>G: route → hitl_gate
        else
            R-->>G: route → extract_memory
        end
    end

    alt Route A: hitl_gate (HITL 1)
        rect rgb(255, 230, 230)
            Note over G,N4a: Node 4a: hitl_gate (LangGraph interrupt)
            G->>N4a: pass state
            N4a->>S: read brd_memory, pending_feedback
            N4a->>N4a: build requirements summary
            N4a->>N4a: interrupt({type: "hitl_1", summary, action_required})
            Note over N4a: ⏸️ Graph PAUSES here
            Note over U,N4a: User sees summary + "Proceed" / "Add More" buttons
            U-->>N4a: Resume with user decision
            alt user says "proceed"
                N4a-->>G: return {mode: "drafting"}
                Note over G: → triggers /generate-brd
            else user says "add more"
                N4a->>S: feedback_gathering = true
                N4a-->>G: return to gathering
            end
        end
    else Route B: extract_memory (continue gathering)
        rect rgb(230, 255, 230)
            Note over G,N4b: Node 4b: extract_memory
            G->>N4b: pass state
            N4b->>S: read last 2 messages (user + assistant)
            N4b->>LM: MemoryStoreManager.extract()
            LM->>LM: merge BRDMemory items
            LM-->>N4b: merged memory dict
            N4b->>S: write brd_memory
            Note over N4b: brd_memory persisted by SqliteSaver
            N4b-->>G: return state updates
        end
    end

    G-->>E: final ChatbotState
    E->>E: build response
    alt hitl_gate paused
        E-->>U: {reply: "HITL 1: Summary...", mode, ready_for_production: true, session_id}
    else feedback_gathering == true
        E-->>U: {reply: "Any more reviews?", mode, feedback_gathering: true, session_id}
    else
        E-->>U: {reply, mode, draft?, turn_id, session_id}
    end
```

## Node Execution Summary

| Step | Node | Reads From State | Writes To State | External I/O | Retry |
|---|---|---|---|---|---|
| 1 | `summarize` | `messages` | `rolling_summary`, `messages` (RemoveMessage) | LLM (conditional) | RetryPolicy(3) |
| 2 | `retrieve_context` | `mode`, `messages`, `brd_memory`, `rolling_summary`, `current_draft`, `last_retrievals`, `pending_feedback`, `feedback_gathering` | `last_retrievals["assembled"]` | Chroma, Neo4j, ArtifactStore | RetryPolicy(2) |
| 3 | `invoke_llm` | `last_retrievals["assembled"]` | `messages`, `reply_text`, `ready_for_production` | LLM + Presidio | RetryPolicy(3) |
| — | `route_after_conversation` | `ready_for_production` | — | — (pure routing) | — |
| 4a | `hitl_gate` | `brd_memory`, `pending_feedback` | `feedback_gathering`, `mode` | LangGraph `interrupt()` | — |
| 4b | `extract_memory` | `messages` (last 2) | `brd_memory` | LangMem Store | — |

## v2 Key Changes

### 1. Conditional Edge Replaces Linear Chain
- **v1:** `invoke_llm → extract_memory → END` (always linear).
- **v2:** `invoke_llm → route_after_conversation → hitl_gate | extract_memory`. The routing function is pure — inspects `ready_for_production` only, no side effects.

### 2. HITL 1 is a LangGraph Interrupt
- **v1:** Readiness was a flag returned to frontend; frontend decided when to call `/generate-brd`.
- **v2:** `hitl_gate` node calls `interrupt()`, which pauses the graph at the checkpointer level. The graph state is persisted to SqliteSaver. When the user responds, the graph resumes from the exact point.

### 3. Presidio Guardrails
- **v1:** No PII scanning.
- **v2:** Input guardrail scans user message before appending to state. Output guardrail scans LLM reply before returning to user.

### 4. Strategy Inference via Semantic Router
```
if feedback_gathering == true:
    → REFINEMENT (or BRD_UPDATE if draft exists)
elif mode == "request_changes":
    → REFINEMENT
else:
    → semantic_router(user_message)  # embedding-based, replaces keyword matching
    → fallback: INFORMATION_GATHERING
```

### 5. Rejection Tracking
When entering via `/request-changes`, the endpoint sets `draft_status = REJECTED` and increments `rejection_count` before invoking the graph. This is logged for observability (no hard cap in PoC).
