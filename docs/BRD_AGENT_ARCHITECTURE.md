# BRD Agent — Full Architecture & Flow Definition

> **Version:** Simplified 3-State Flow (as of 2026-06-04)  
> **Purpose:** Written reference of the complete BRD Agent workflow before making improvements.

---

## 1. System Overview

The BRD Agent is a **3-state conversational system** that guides a user through eliciting business requirements and eventually emits a structured, schema-validated Business Requirements Document (BRD). It is built on **LangGraph** with two compiled graphs sharing state, LangMem memory extraction, RAG (vector + knowledge graph), and OpenTelemetry tracing.

**Core Concepts:**
- **Session:** A single BRD authoring thread, identified by `session_id` (maps to LangGraph `thread_id`).
- **Three States:** The system operates in exactly three states that loop: **🟡 Information Gathering** → **🟢 BRD Production** → **🔵 Review Mode** → (back to 🟡 if changes needed).
- **Universal Gathering:** 🟡 Gathering is the **only entry point** for all user input — initial prompts, clarifications, and review changes all enter here.
- **Atomic Production (per cycle):** 🟢 BRD Production is **atomic** — within a single production cycle, the document is produced or updated **all at once** with the complete gathered context. Nothing is emitted mid-gathering. The user may run **multiple production cycles** (initial generation, then updates after feedback) until satisfied.
- **Feedback Gathering:** If the user wants changes in 🔵 Review Mode, the agent asks *"Any more reviews?"* and keeps looping inside 🟡 Gathering until the user says no. Then it exits to 🟢 Production and applies everything at once.
- **State:** `ChatbotState` is the per-thread LangGraph state. All substantive data lives here; `SessionState` is only a thin approval flag.
- **Graph:** Two LangGraph `StateGraph` instances share one `MemorySaver` checkpointer:
  - **Gathering Graph** — multi-turn conversation with memory extraction.
  - **Drafting Graph** — single-shot structured BRD generation (or update).

---

## 2. State Structure (`ChatbotState`)

| Field | Type | Reducer | Purpose |
|---|---|---|---|
| `messages` | `list[BaseMessage]` | `add_messages` | Running transcript. Trimmed by `summarize` node when > 12 turns. |
| `mode` | `ChatMode` | overwrite | `gathering` \| `drafting` \| `request_changes` |
| `brd_memory` | `dict` \| `None` | overwrite | Merged `BRDMemory` extracted from conversation via LangMem. |
| `current_draft` | `dict` \| `None` | overwrite | Validated `BRDResponse` JSON after drafting. |
| `reply_text` | `str` \| `None` | overwrite | Plain-text assistant reply from last turn (gathering only). |
| `rolling_summary` | `dict` \| `None` | overwrite | `ConversationSummary` of trimmed older turns. |
| `last_retrievals` | `dict` \| `None` | overwrite | Per-turn assembler output, caches, and `draft_raw`. |
| `pending_feedback` | `list[str]` | add_items | **NEW:** Accumulates review feedback items while in 🟡 Gathering before atomic production. |
| `feedback_gathering` | `bool` | overwrite | **NEW:** Flag indicating we are in the "Any more reviews?" feedback collection loop. |

**Key Memory Schemas:**
- **BRDMemory** (extracted facts): title, background_notes, objectives, stakeholders, functional_requirements, non_functional_requirements, constraints, assumptions, risks, open_questions.
- **ConversationSummary** (compressed history): recent_topics, decisions_so_far, pending_clarifications, compressed_narrative.
- **BRDResponse** (final document): brd_id, title, background, objectives, stakeholders, functional_requirements, non_functional_requirements, acceptance_criteria, assumptions, out_of_scope, dependencies, risks, drafted_at, drafted_by.

---

## 3. The Three States (User Journey)

### 🟡 State 1 — INFORMATION GATHERING (universal entry point)
**Trigger:** Any user message via `POST /chat`.

**Behavior:**
- **Initial entry:** User provides prompt + optional document. Document is ingested into Vector DB. Agent analyzes, retrieves from VDB + KG, and asks clarifying questions.
- **Clarification loop:** Agent keeps asking questions until enough information is collected (`DEC1: Enough Info?`).
- **Review re-entry:** When the user requests changes from 🔵 Review Mode, the agent enters feedback gathering. It asks *"Any more reviews?"* and appends each feedback item to `pending_feedback`. The loop continues until the user says no — then the agent auto-transitions to 🟢 Production.

**Flow:**
1. **Endpoint** (`/chat`) receives `{message}`.
2. **Mode decision:**
   - If `feedback_gathering == true` → continue feedback collection.
   - If `current_draft` exists and is unapproved → switch to `request_changes` (enters feedback gathering loop).
   - Else → `gathering`.
3. **HumanMessage** appended to `state.messages`.
4. **LangGraph thread invocation** on the **Gathering Graph**.
5. **Node 1: `summarize`**
   - If `len(messages) > 12`: summarize oldest slice (everything except last 8) via LLM into `ConversationSummary`, merge with existing `rolling_summary`, emit `RemoveMessage` to drop old turns.
   - Else: no-op.
6. **Node 2: `retrieve_context`**
   - Infer `ContextStrategy` from `mode` + user message + `feedback_gathering` + draft existence.
   - `ContextAssembler.assemble()` builds the prompt stack:
     - System prompt (gathering, drafting, or request_changes variant)
     - Filtered `BRDMemory` block
     - `rolling_summary` block
     - Doc-retrieval hits from Chroma (top-k by strategy)
     - KG-retrieval hits from Neo4j (entities + 1-hop neighbours)
     - Artifact summaries
     - **Draft + pending_feedback block** (if review re-entry)
     - Sliced conversation turns
   - Store assembled messages + token accounting in `state.last_retrievals["assembled"]`.
7. **Node 3: `invoke_llm`**
   - Read assembled messages, call LLM (`temperature=0.4`, `max_tokens=800`).
   - If `feedback_gathering` and user says "no more reviews": set `feedback_gathering = false` and queue auto-transition to 🟢 Production.
   - Append `AIMessage` to `state.messages`.
   - Store plain text in `state.reply_text`.
8. **Node 4: `extract_memory`**
   - Take last user + assistant exchange.
   - Send to LangMem `MemoryStoreManager` (namespace `brd_agent/<session_id>`).
   - Extract/update `BRDMemory` items.
   - Read merged memory back into `state.brd_memory`.
9. **Graph END.**
10. **Response:** `{reply, mode, draft?, turn_id, ready_for_production?}`.

**Exit condition to 🟢 Production:**
- For initial gathering: Agent decides "Enough Info?" (LLM judgment or hard heuristic). If yes, the agent **pauses for HITL 1** — it presents a summary of gathered requirements to the user and asks for explicit confirmation before proceeding to production.
- For feedback gathering: User explicitly signals "no more reviews" while `feedback_gathering == true`. Then the agent presents a summary of all collected feedback and asks for HITL 1 confirmation before updating.

---

### 🟢 State 2 — BRD PRODUCTION (atomic generate or update)
**Trigger:** HITL 1 approval from 🟡 Gathering. After the agent decides "Enough Info? = Yes" (or feedback collection is complete), it presents a summary to the user. The user must explicitly confirm before the system transitions to Production.

**Preconditions:** Sufficient information in `brd_memory` + `messages` + `pending_feedback` (if re-entry).

**Behavior:** Within a single production cycle, the BRD is produced or updated **atomically** (all at once) with the complete gathered context. No incremental emission. The user may run **multiple production cycles** until satisfied.

**Flow:**
1. **Endpoint** (`/generate-brd`) is called (either by user clicking "Generate BRD" or by auto-transition from feedback gathering).
2. Set `mode = "drafting"`.
3. **LangGraph thread invocation** on the **Drafting Graph** (no new `HumanMessage`).
4. **Node 1: `retrieve_context`**
   - `infer_strategy()` returns `BRD_GENERATION` (or `BRD_UPDATE` if `current_draft` exists).
   - Assembler uses `DRAFTING_SYSTEM_PROMPT` (or `UPDATE_SYSTEM_PROMPT`).
   - Includes full memory, all conversation turns, rolling summary, **and all accumulated `pending_feedback`**.
   - Heavy doc retrieval (4 hits) and KG retrieval (4 entities with 1-hop neighbours).
   - Instructs LLM to produce strict JSON matching `BRDResponse` schema.
   - Store in `state.last_retrievals["assembled"]`.
5. **Node 2: `draft_llm`**
   - Read assembled messages.
   - Call LLM (`response_format={"type": "json_object"}`, `temperature=0.2`, `max_tokens=4000`).
   - Store raw JSON output in `state.last_retrievals["draft_raw"]`.
6. **Node 3: `schema_validate`**
   - Extract JSON from raw (handles markdown fences, non-JSON wrappers).
   - Inject `drafted_by = "brd-agent@0.1.0"`.
   - Validate against `BRDResponse` Pydantic model.
   - **Success:** Write validated dict to `state.current_draft`. Clear `pending_feedback`. Mark span OK.
   - **Failure:** Mark span ERROR. Raise exception (graph halts; draft not saved).
7. **Graph END.**
8. **Auto-transition to 🔵 Review Mode.**
9. **Response:** `{draft: BRDResponse, mode: "awaiting_approval"}`.

---

### 🔵 State 3 — REVIEW MODE (HITL 2)
**Trigger:** Auto-transition from 🟢 Production after successful drafting.

**Behavior:** User reviews the delivered BRD. This is **HITL 2** — the user must actively decide whether to accept or request changes. Two paths:

**Path A — Accept (HITL 2 = No Changes):**
1. User clicks "Approve" → `POST /approve`.
2. Endpoint marks `session.approved = True`.
3. Response: `{status: "approved", brd_id}`.
4. **END.**

**Path B — Request Changes (HITL 2 = Changes Needed):**
1. User enters feedback → `POST /request-changes` with `{feedback}`.
2. Endpoint:
   - Appends feedback to `pending_feedback`.
   - Sets `feedback_gathering = true`.
   - Invokes **Gathering Graph** with `mode="request_changes"`.
3. `retrieve_context` infers `REFINEMENT` strategy + feedback gathering mode.
4. Assembler injects `current_draft` + **all** `pending_feedback` into the prompt.
5. `invoke_llm` acknowledges the feedback and asks *"Any more reviews?"*
6. `extract_memory` updates `BRDMemory` with any new facts from the feedback.
7. **Loop:** If user provides more feedback, it is appended to `pending_feedback` and step 2 repeats. If user says "no", `feedback_gathering` is cleared and the system **auto-transitions** to 🟢 Production.
8. Response: `{reply, draft, mode}`.

---

## 4. Node Reference

| Node | Graph | Input State | Output State | External Calls |
|---|---|---|---|---|
| `summarize` | Gathering | `messages` | `rolling_summary`, `messages` (with `RemoveMessage`) | LLM (summary generation) |
| `retrieve_context` | Both | `mode`, `messages`, `brd_memory`, `rolling_summary`, `current_draft`, `last_retrievals`, `pending_feedback`, `feedback_gathering` | `last_retrievals["assembled"]` | Chroma, Neo4j, ArtifactStore |
| `invoke_llm` | Gathering | `last_retrievals["assembled"]` | `messages`, `reply_text` | LLM (chat completion) |
| `extract_memory` | Gathering | `messages` (last 2 turns) | `brd_memory` | LangMem MemoryStoreManager |
| `draft_llm` | Drafting | `last_retrievals["assembled"]` | `last_retrievals["draft_raw"]` | LLM (JSON mode, 4k tokens) |
| `schema_validate` | Drafting | `last_retrievals["draft_raw"]` | `current_draft`, clears `pending_feedback` | Pydantic validation |

---

## 5. Context Strategy Matrix

The `ContextAssembler` decides what to include based on `ContextStrategy`:

| Strategy | Turns | Summary | Memory Filter | Draft Inject | Pending Feedback | Doc Hits | KG Hits |
|---|---|---|---|---|---|---|---|
| `INFORMATION_GATHERING` | 6 | Yes | Full | No | No | 2 | 2 |
| `CLARIFICATION` | 10 | Yes | Full | No | No | 2 | 2 |
| `REFINEMENT` | 10 | Yes | Full | Yes | **All items** | 2 | 2 |
| `BRD_GENERATION` | All | Yes | Full | No | No | 4 | 4 |
| `BRD_UPDATE` | All | Yes | Full | Yes (current draft) | **All items** | 4 | 4 |
| `TEMPLATE_GUIDED` | 6 | Yes | Full | No | No | 2 | 2 + template |
| `COMPLIANCE_HEAVY` | 6 | Yes | Full | No | No | 2 | 2 + regulatory |

**Inference Priority:**
1. If `feedback_gathering == true` → `REFINEMENT` (or `BRD_UPDATE` if draft exists).
2. Explicit `mode` override (`drafting` → `BRD_GENERATION`; `request_changes` → `REFINEMENT`).
3. Keyword matching on user message ("template", "compliance", "clarify", etc.).
4. Default → `INFORMATION_GATHERING`.

---

## 6. Data Sources & Retrieval Flow

### 6.1 Vector Store (Chroma)
- **Content:** BRD templates, glossary entries, synthetic prior BRDs, **and ingested user documents**.
- **Embedding:** ONNX MiniLM-L6-v2 (local, no API key).
- **Usage:** Query by assembled context; return top-k document chunks.

### 6.2 Knowledge Graph (Neo4j)
- **Schema:** Entities = `Stakeholder`, `BusinessUnit`, `RegulatoryDomain`, `System`. Relations = `WORKS_IN`, `OVERSEES`, `SUBJECT_TO`, `INTEGRATES_WITH`.
- **Query:** Token-based substring matching for entity linking → 1-hop traversal → return entities + neighbours.

### 6.3 Artifact Store
- In-memory dict for large tool outputs (e.g., fetched BRD templates, ingested documents).
- Only `ArtifactRef` (summary) surfaces in prompt; full content retrieved on demand.

---

## 7. Memory Lifecycle

```
User Message → Assistant Reply
       ↓
   extract_memory node
       ↓
LangMem MemoryStoreManager (namespace: brd_agent/<session_id>)
       ↓
   BRDMemory items (extracted facts)
       ↓
Merged back into state.brd_memory
       ↓
retrieve_context includes brd_memory in next prompt
       ↓
LLM uses accumulated facts to ask better questions / draft BRD
```

**Feedback Gathering Extension:**
```
User Review Feedback → /request-changes
       ↓
   append to state.pending_feedback
       ↓
   feedback_gathering = true
       ↓
   invoke_llm asks "Any more reviews?"
       ↓
   Loop until user says "no"
       ↓
   feedback_gathering = false
       ↓
   Auto-transition to /generate-brd
       ↓
   retrieve_context includes ALL pending_feedback in prompt
       ↓
   draft_llm produces updated BRD atomically
```

---

## 8. Telemetry & Tracing

- **OpenTelemetry + Phoenix** (`localhost:6006`).
- Every node and endpoint opens spans with:
  - `session.id`, `agent.id`
  - `openinference.span.kind`
  - Prompt lineage, token accounting
- OpenAI auto-instrumentation captures LLM calls as child spans.
- Traces exported via OTLP/HTTP.

---

## 9. API Surface

| Endpoint | Method | Body | Response | Graph / Action |
|---|---|---|---|---|
| `/health` | GET | — | `{ok, agent_id, version}` | — |
| `/chat` | POST | `{message}` | `{reply, mode, draft?, turn_id, ready_for_production?}` | Gathering |
| `/generate-brd` | POST | — | `{draft, mode}` | Drafting (atomic) |
| `/approve` | POST | — | `{status, brd_id}` | Session flag |
| `/request-changes` | POST | `{feedback}` | `{reply, draft, mode, feedback_gathering}` | Gathering (feedback loop) |
| `/reset` | POST | — | `{status, session_id}` | New thread |
| `/memory` | GET | — | `{session_id, memory}` | Read state / LangMem |
| `/tools/fetch-brd-template` | POST | `{template_name?}` | `{artifact_ref, available_templates, note}` | ArtifactStore + transcript |

---

## 10. Key Design Rules (Simplified Flow)

1. **🟡 Gathering is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic** — BRD is produced/updated **once** per cycle with the complete gathered context.
3. **Feedback gathering** — If user wants changes at HITL 2, agent asks *"Any more reviews?"* and keeps looping inside 🟡 Gathering until user says no. Then it exits to 🟢 Production and applies everything at once.
4. **HITL 1 gate is mandatory** — The agent cannot auto-jump to 🟢 Production. After deciding "Enough Info? = Yes", it must pause, present a summary of gathered requirements, and get explicit user approval. This prevents premature drafting.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from HITL 2 is the only backward transition triggered by the user.

---

## 11. Known Gaps / Improvement Areas (Pre-Change Baseline)

1. ~~**No human-in-the-loop gate before drafting:**~~ ✅ **ADDRESSED by HITL 1** — Agent now pauses after "Enough Info?" and presents a summary for user confirmation before entering Production. Prevents premature drafting.
2. **No conditional edges in graphs:** All graphs are linear chains. No branching on validation failure, no retry loops on schema errors.
3. **Drafting graph does not use conversation memory extraction:** `draft_llm` and `schema_validate` are pure transform nodes; no memory is written during drafting.
4. **Rolling summary is lossy:** Trimmed messages are gone forever (only summary remains). No way to "unroll" if needed.
5. **Single-shot drafting:** No progressive / section-by-section draft generation. A 4k token limit could truncate complex BRDs.
6. **Schema validation raises on failure:** The graph halts; the user gets an error, not a graceful degradation or auto-retry.
7. **Context strategy inference is keyword-based:** No semantic classification; brittle to phrasing.
8. **Approval is a session flag only:** No versioning of drafts (overwriting `current_draft` on re-generation loses previous version).
9. **"Enough Info?" is LLM-judged:** No hard requirements-coverage checklist before allowing transition to production.

---

## Related Diagrams

- [Detailed Flow Diagram](diagrams/detailed_flow.md) — Data flow through every node and state field.
- [State Flow Diagram](diagrams/state_flow.md) — Mode transitions and state machine (3-state simplified).
- [Gathering Graph Sequence](diagrams/gathering_graph.md) — Step-by-step node execution (includes feedback gathering loop).
- [Drafting Graph Sequence](diagrams/drafting_graph.md) — Step-by-step node execution (atomic production).
- [Context Assembly Flow](diagrams/context_assembly.md) — How the prompt is built.
- [Memory Lifecycle](diagrams/memory_lifecycle.md) — How memory is extracted, stored, and reused.
