# Gathering Graph Sequence Diagram (Universal Entry + Feedback Gatherings)

This diagram shows the exact step-by-step execution inside the **Gathering Graph**, which is now the **universal entry point** for all user input — initial prompts, clarifications, and review feedback.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant E as /chat or /request-changes Endpoint
    participant S as ChatbotState
    participant G as LangGraph Thread
    participant N1 as summarize
    participant N2 as retrieve_context
    participant N3 as invoke_llm
    participant N4 as extract_memory
    participant LLM as LLM Client
    participant CH as Chroma
    participant NEO as Neo4j
    participant ART as ArtifactStore
    participant LM as LangMem Store

    U->>E: POST /chat {message}
    Note over E: OR POST /request-changes {feedback}

    E->>E: Mode decision
    alt initial chat
        E->>S: mode = gathering
    else review re-entry
        E->>S: mode = request_changes
        E->>S: feedback_gathering = true
        E->>S: append feedback to pending_feedback
    end
    E->>S: append HumanMessage to messages
    E->>G: invoke with ChatbotState

    rect rgb(230, 245, 255)
        Note over G,N1: Node 1: summarize
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
        Note over G,N2: Node 2: retrieve_context
        G->>N2: pass state
        N2->>S: read mode, messages, brd_memory,<br/>rolling_summary, current_draft, last_retrievals,<br/>pending_feedback, feedback_gathering
        N2->>N2: infer ContextStrategy
        alt feedback_gathering == true
            N2->>N2: strategy = REFINEMENT (or BRD_UPDATE)
        else mode == request_changes
            N2->>N2: strategy = REFINEMENT
        else keyword match
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
        Note over G,N3: Node 3: invoke_llm
        G->>N3: pass state
        N3->>S: read last_retrievals["assembled"]["messages"]
        N3->>LLM: chat completion (temp=0.4, max_tokens=800)
        LLM-->>N3: AIMessage content
        alt enough_info == true OR (feedback_gathering == true AND user says "no more reviews")
            N3->>S: feedback_gathering = false (if applicable)
            N3->>E: queue HITL 1 — present summary for user confirmation
            Note over N3: Agent asks: "I've gathered enough info.
            Note over N3: Here's a summary. Shall I proceed?"
        else
            Note over N3: Continue gathering / feedback collection
        end
        N3->>S: append AIMessage to messages
        N3->>S: write reply_text
        N3-->>G: return state updates
    end

    rect rgb(230, 255, 230)
        Note over G,N4: Node 4: extract_memory
        G->>N4: pass state
        N4->>S: read last 2 messages (user + assistant)
        N4->>LM: MemoryStoreManager.extract()
        LM->>LM: merge BRDMemory items
        LM-->>N4: merged memory dict
        N4->>S: write brd_memory
        N4-->>G: return state updates
    end

    G-->>E: final ChatbotState
    E->>E: build response
    alt feedback_gathering == true
        E-->>U: {reply: "Any more reviews?", mode, feedback_gathering: true}
    else if ready_for_production == true
        E-->>U: {reply: "HITL 1: Here's a summary... Proceed?", mode, ready_for_production: true}
    else
        E-->>U: {reply, mode, draft?, turn_id}
    end
```

## Node Execution Summary

| Step | Node | Reads From State | Writes To State | External I/O |
|---|---|---|---|---|
| 1 | `summarize` | `messages` | `rolling_summary`, `messages` (RemoveMessage) | LLM (conditional) |
| 2 | `retrieve_context` | `mode`, `messages`, `brd_memory`, `rolling_summary`, `current_draft`, `last_retrievals`, `pending_feedback`, `feedback_gathering` | `last_retrievals["assembled"]` | Chroma, Neo4j, ArtifactStore |
| 3 | `invoke_llm` | `last_retrievals["assembled"]` | `messages`, `reply_text`, `feedback_gathering` (conditional) | LLM |
| 4 | `extract_memory` | `messages` (last 2) | `brd_memory` | LangMem Store |

## Key Differences from Previous Version

### 1. Universal Entry Point
- **Before:** `/chat` and `/request-changes` were separate paths with different semantics.
- **Now:** Both endpoints route through the **same Gathering Graph**. `/request-changes` simply sets `feedback_gathering = true` and appends feedback to `pending_feedback` before entering the graph.

### 2. Feedback Gathering Loop
- **Before:** Each `/request-changes` call immediately triggered a new draft generation after one turn of discussion.
- **Now:** The agent asks *"Any more reviews?"* and loops inside Gathering until the user explicitly signals completion. All feedback is accumulated in `pending_feedback` and applied atomically in the next Production cycle.

### 3. Strategy Inference Priority
```
if feedback_gathering == true:
    → REFINEMENT (or BRD_UPDATE if draft exists)
elif mode == "request_changes":
    → REFINEMENT
else:
    → keyword-based or default
```

### 4. Prompt Injection for Reviews
When `REFINEMENT` or `BRD_UPDATE` strategy is active, the assembler injects:
- The current draft JSON (so the LLM knows what it's updating)
- **ALL items in `pending_feedback`** (not just the latest one)
- A system instruction to ask "Any more reviews?" if `feedback_gathering` is still true.
