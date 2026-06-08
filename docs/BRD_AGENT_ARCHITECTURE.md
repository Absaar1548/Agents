# BRD Agent — Full Architecture & Flow Definition

> **Version:** v2.0 — Production-Aligned (as of 2026-06-08)  
> **Purpose:** Complete architecture specification for the BRD Agent, aligned to the [MDP Agent Chassis Framework v1.0](Team%20Docs/MDP_Agent_Chassis_Framework_Design_v1_0.txt).  
> **Previous:** v1.0 was the pre-improvement 3-state baseline.

### Chassis Plane Alignment

| Chassis Plane | PoC Implementation | Production Target |
|:---|:---|:---|
| P1: Identity & Access | Request-scoped `session_id` | Managed Identity + OBO tokens |
| P2: Orchestration & Routing | LangGraph + conditional edges + SqliteSaver | LangGraph + PostgresSaver |
| P3: Memory & State | InMemoryStore (LangMem) + SqliteSaver (checkpointer) | PostgresStore + PostgresSaver |
| P4: Tool Access (MCP) | Direct retriever calls (KG/VDB are not MCP per Chassis §3.3.1) | Same |
| P5: Guardrails & Safety | Presidio (open-source) — input/output wrappers | Presidio + Azure AI Content Safety |
| P6: LLM Gateway | Direct Azure OpenAI / Ollama client | APIM AI Gateway |
| P7: Observability | OpenTelemetry + Phoenix + draft versioning | LangSmith + Event Hub + Databricks |
| P8: Human-in-the-Loop | LangGraph `interrupt()` + Streamlit buttons | LangGraph interrupts + Agent Ops Portal |

---

## 1. System Overview

The BRD Agent is a **3-state conversational system** that guides a user through eliciting business requirements and eventually emits a structured, schema-validated Business Requirements Document (BRD). It is built on **LangGraph** with two compiled graphs sharing state, LangMem memory extraction, RAG (vector + knowledge graph), Presidio guardrails, and OpenTelemetry tracing.

**Core Concepts:**
- **Session:** A single BRD authoring thread, identified by `session_id` (maps to LangGraph `thread_id`). Each request carries its own `session_id` — no global singleton.
- **Three States:** The system operates in exactly three states that loop: **🟡 Agent Interaction** → **🟢 BRD Production** → **🔵 Review Mode** → (back to 🟡 if changes needed).
- **Universal Agent Interaction:** 🟡 Agent Interaction is the **only entry point** for all user input — initial prompts, clarifications, and review changes all enter here.
- **Atomic Production (per cycle):** 🟢 BRD Production is **atomic** — within a single production cycle, the document is produced or updated **all at once** with the complete gathered context. Nothing is emitted mid-gathering. The user may run **multiple production cycles** (initial generation, then updates after feedback) until satisfied.
- **Feedback Re-entry is not a separate mode:** If the user wants changes in 🔵 Review Mode, the agent asks *"Any more reviews?"* and keeps looping inside 🟡 Agent Interaction (with the current draft in context) until the user says no. Then it reaches HITL 1 before production. The user may attach supporting documents with feedback, which are ingested through the same pipeline.
- **HITL Gates are LangGraph interrupts:** Both HITL 1 (pre-production) and HITL 2 (post-production review) are implemented as native LangGraph `interrupt()` calls, not ad-hoc branches. This is a hard constraint per Chassis §3.2.2.
- **Guardrails:** Presidio input/output wrappers scan every LLM call for PII (Chassis §3.5). Financial identifiers (SSN, credit cards, bank numbers) and contact details (email, phone) are actively masked. Person names and IPs are logged but not masked (BRDs legitimately contain stakeholder names).
- **State:** `ChatbotState` is the per-thread LangGraph state. All substantive data lives here (including `draft_status` and `rejection_count` — previously tracked as a thin session flag).
- **Graph:** Two LangGraph `StateGraph` instances share one `SqliteSaver` checkpointer (production: `PostgresSaver`):
  - **Agent Interaction Graph** — multi-turn conversation with memory extraction, uses **conditional edges** for routing.
  - **Drafting Graph** — structured BRD generation with **validation retry loop**.
- **Draft Versioning:** Every production cycle appends an immutable entry to `draft_history` with full provenance (prompt hash, context strategy, model, token accounting).

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
| `pending_feedback` | `list[str]` | add_items | Accumulates review feedback items while in 🟡 Agent Interaction before atomic production. |
| `feedback_gathering` | `bool` | overwrite | Flag indicating we are in the "Any more reviews?" feedback collection loop. |
| `ready_for_production` | `bool` | overwrite | **NEW (v2):** Set by `invoke_llm` when sufficient detail is gathered. Drives the conditional edge to `hitl_gate`. |
| `retry_count` | `int` | overwrite | **NEW (v2):** Tracks schema validation retries in the drafting graph. Max 2 retries. |
| `validation_errors` | `list[str]` | overwrite | **NEW (v2):** Pydantic validation error details, fed back to LLM on retry. |
| `draft_status` | `DraftStatus` | overwrite | **NEW (v2):** `draft` \| `approved` \| `rejected`. Replaces the old `session.approved` flag. |
| `rejection_count` | `int` | overwrite | **NEW (v2):** Tracks how many times the user has rejected a draft. Logged for observability (no hard cap in PoC). |
| `draft_history` | `list[dict]` | `add_items` | **NEW (v2):** Append-only list of draft snapshots with full provenance (version, prompt hash, strategy, model, token accounting, status). |

**Enums:**
```python
class DraftStatus(str, Enum):
    DRAFT = "draft"          # Just produced, awaiting review
    APPROVED = "approved"    # Reviewer approved
    REJECTED = "rejected"    # Reviewer rejected with feedback
```

**Key Memory Schemas:**
- **BRDMemory** (extracted facts): title, background_notes, objectives, stakeholders, functional_requirements, non_functional_requirements, constraints, assumptions, risks, open_questions.
- **ConversationSummary** (compressed history): recent_topics, decisions_so_far, pending_clarifications, compressed_narrative.
- **BRDResponse** (final document): brd_id, title, background, objectives, stakeholders, functional_requirements, non_functional_requirements, acceptance_criteria, assumptions, out_of_scope, dependencies, risks, drafted_at, drafted_by.

---

## 3. The Three States (User Journey)

### 🟡 State 1 — AGENT INTERACTION (universal entry point)
**Trigger:** Any user message via `POST /chat`.

**Behavior:**
- **Initial entry:** User provides prompt + optional document. Document is ingested into Vector DB. Agent analyzes, retrieves from VDB + Enterprise KG, and asks clarifying questions.
- **Clarification loop:** Agent keeps asking questions until sufficient detail is gathered (`DEC1: Sufficient detail gathered?`).
- **Review re-entry:** When the user requests changes from 🔵 Review Mode, the agent re-enters 🟡 Agent Interaction with the current draft in context. It asks *"Any more reviews?"* and appends each feedback item to `pending_feedback`. The user may attach supporting documents (ingested through the same pipeline). The loop continues until the user says no — then the agent reaches HITL 1.

**Flow:**
1. **Endpoint** (`/chat`) receives `{message, session_id}`.
2. **Mode decision:**
   - If `feedback_gathering == true` → continue feedback collection.
   - If `current_draft` exists and `draft_status != approved` → switch to `request_changes` (enters feedback gathering loop).
   - Else → `gathering`.
3. **Presidio Input Guardrail** (Chassis §3.5): Scan user message for PII. Mask financial/contact identifiers. Log person name detections.
4. **HumanMessage** appended to `state.messages`.
5. **LangGraph thread invocation** on the **Agent Interaction Graph**.
6. **Node 1: `summarize`**
   - If `len(messages) > 12`: summarize oldest slice (everything except last 8) via LLM into `ConversationSummary`, merge with existing `rolling_summary`, emit `RemoveMessage` to drop old turns.
   - Else: no-op.
7. **Node 2: `retrieve_context`**
   - Infer `ContextStrategy` from `mode` + user message + `feedback_gathering` + draft existence. (v2: semantic router replaces keyword matching for intent classification.)
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
8. **Node 3: `invoke_llm`**
   - Read assembled messages, call LLM (`temperature=0.4`, `max_tokens=800`).
   - **Presidio Output Guardrail:** Scan LLM reply for PII leakage before returning.
   - If LLM judges sufficient detail gathered: set `ready_for_production = true`.
   - If `feedback_gathering` and user says "no more reviews": set `feedback_gathering = false`, `ready_for_production = true`.
   - Append `AIMessage` to `state.messages`.
   - Store plain text in `state.reply_text`.
9. **Conditional Edge: `route_after_conversation`** (v2 — replaces linear chain):
   ```
   if ready_for_production == true → hitl_gate
   else → extract_memory (continue gathering)
   ```
10. **Node 4a (if routed): `hitl_gate`** (HITL 1 — LangGraph `interrupt()`)
    - Build a summary of gathered requirements (or collected feedback).
    - Call `interrupt({"type": "hitl_1", "summary": summary, "action_required": "confirm_or_add_more"})`.
    - Graph **pauses**. Frontend shows summary + "Proceed" / "Add More" buttons.
    - On resume: if user says "add more" → set `feedback_gathering = true`, continue gathering. If user confirms → proceed to production.
11. **Node 4b (if routed): `extract_memory`**
    - Take last user + assistant exchange.
    - Send to LangMem `MemoryStoreManager` (namespace `brd_agent/<session_id>`).
    - Extract/update `BRDMemory` items.
    - Read merged memory back into `state.brd_memory`.
12. **Graph END.**
13. **Response:** `{reply, mode, draft?, turn_id, ready_for_production?}`.

**Graph Topology (v2):**
```
START → summarize → retrieve_context → invoke_llm
                                          │
                             route_after_conversation
                               ┌─────────┴─────────┐
                               │                   │
                         extract_memory         hitl_gate
                               │               (interrupt)
                              END
```

**Exit condition to 🟢 Production:**
- For initial agent interaction: Agent decides "Sufficient detail gathered?" (LLM judgment). If yes, the conditional edge routes to `hitl_gate`, which **pauses via `interrupt()`** and presents a summary for user confirmation.
- For feedback collection: User explicitly signals "no more reviews" while `feedback_gathering == true`. The conditional edge routes to `hitl_gate` with a summary of all collected feedback.

---

### 🟢 State 2 — BRD PRODUCTION (atomic generate or update)
**Trigger:** HITL 1 approval from 🟡 Agent Interaction. After the `hitl_gate` node resumes with user confirmation, the system transitions to Production.

**Preconditions:** Sufficient information in `brd_memory` + `messages` + `pending_feedback` (if re-entry).

**Behavior:** Within a single production cycle, the BRD is produced or updated **atomically** (all at once) with the complete gathered context. No incremental emission. The user may run **multiple production cycles** until satisfied.

**Flow:**
1. **Endpoint** (`/generate-brd`) is called (either by user clicking "Generate BRD" or by auto-transition from HITL 1 approval).
2. Set `mode = "drafting"`, `retry_count = 0`.
3. **LangGraph thread invocation** on the **Drafting Graph** (no new `HumanMessage`).
4. **Node 1: `retrieve_context`**
   - `infer_strategy()` returns `BRD_GENERATION` (or `BRD_UPDATE` if `current_draft` exists).
   - Assembler uses `DRAFTING_SYSTEM_PROMPT` (or `UPDATE_SYSTEM_PROMPT`).
   - Includes full memory, all conversation turns, rolling summary, **and all accumulated `pending_feedback`**.
   - Heavy doc retrieval (4 hits) and KG retrieval (4 entities with 1-hop neighbours).
   - Instructs LLM to produce strict JSON matching `BRDResponse` schema.
   - **If retrying:** also injects `validation_errors` from previous attempt so the LLM can self-correct.
   - Store in `state.last_retrievals["assembled"]`.
5. **Node 2: `draft_llm`**
   - **Presidio Input Guardrail:** Scan assembled context for PII before sending to LLM.
   - Read assembled messages.
   - Call LLM (`response_format={"type": "json_object"}`, `temperature=0.2`, `max_tokens=4000`).
   - Store raw JSON output in `state.last_retrievals["draft_raw"]`.
6. **Node 3: `schema_validate`**
   - Extract JSON from raw (handles markdown fences, non-JSON wrappers).
   - Inject `drafted_by = "brd-agent@0.1.0"`.
   - Validate against `BRDResponse` Pydantic model.
   - **Presidio Output Guardrail:** Scan validated BRD for PII leakage.
   - **Success:** Write validated dict to `state.current_draft`. Set `draft_status = DRAFT`. Clear `pending_feedback`, `validation_errors`. Append to `draft_history`. Mark span OK.
   - **Failure:** Increment `retry_count`. Store error details in `validation_errors`. Mark span WARNING.
7. **Conditional Edge: `route_after_validation`** (v2 — replaces linear chain):
   ```
   if current_draft is valid → END (success)
   if retry_count < 2       → draft_llm (retry with error feedback)
   else                     → error_handler (graceful degradation)
   ```
8. **On success:** Auto-transition to 🔵 Review Mode via `interrupt_after=["schema_validate"]` (HITL 2).
9. **Response:** `{draft: BRDResponse, mode: "awaiting_approval", version: N}`.

**Graph Topology (v2):**
```
START → retrieve_context → draft_llm → schema_validate
                                            │
                               route_after_validation
                           ┌──────────┴─────────┐────────┐
                           │                   │         │
                          END              draft_llm  error_handler
                       (success)           (retry)    (max retries)
```

**Draft History Entry (appended on each successful production):**
```python
{
    "version": N,
    "draft": brd.model_dump(mode="json"),
    "produced_at": "2026-06-08T12:00:00Z",
    "prompt_hash": "sha256:abc123...",
    "context_strategy": "BRD_GENERATION",
    "model": "gpt-4o",
    "token_accounting": {...},
    "status": "draft",
    "reviewed_by": None,
    "reviewed_at": None,
}
```

---

### 🔵 State 3 — REVIEW MODE (HITL 2)
**Trigger:** Auto-transition from 🟢 Production after successful drafting (via `interrupt_after=["schema_validate"]`).

**Behavior:** User reviews the delivered BRD. This is **HITL 2** — the user must actively decide whether to accept or request changes. Two paths:

**Path A — Accept (HITL 2 = No Changes):**
1. User clicks "Approve" → `POST /approve`.
2. Endpoint sets `draft_status = APPROVED` in `ChatbotState` (v2: replaces `session.approved` flag).
3. Updates the latest entry in `draft_history` with `reviewed_by` and `reviewed_at`.
4. Response: `{status: "approved", brd_id, version}`.
5. **END.**

**Path B — Request Changes (HITL 2 = Changes Needed):**
1. User enters feedback → `POST /request-changes` with `{feedback}`.
2. Endpoint:
   - Appends feedback to `pending_feedback`.
   - Sets `feedback_gathering = true`.
   - Sets `draft_status = REJECTED`, increments `rejection_count`.
   - Invokes **Agent Interaction Graph** with `mode="request_changes"`.
3. `retrieve_context` infers `REFINEMENT` strategy + feedback gathering mode.
4. Assembler injects `current_draft` + **all** `pending_feedback` into the prompt.
5. `invoke_llm` acknowledges the feedback and asks *"Any more reviews?"*
6. `extract_memory` updates `BRDMemory` with any new facts from the feedback.
7. **Loop:** If user provides more feedback, it is appended to `pending_feedback` and step 2 repeats. If user says "no", the conditional edge routes to `hitl_gate` (HITL 1) with a summary of planned changes.
8. Response: `{reply, draft, mode, feedback_gathering}`.

---

## 4. Node Reference

| Node | Graph | Input State | Output State | External Calls | Retry |
|---|---|---|---|---|---|
| `summarize` | Agent Interaction | `messages` | `rolling_summary`, `messages` (with `RemoveMessage`) | LLM (summary generation) | RetryPolicy(3) |
| `retrieve_context` | Both | `mode`, `messages`, `brd_memory`, `rolling_summary`, `current_draft`, `last_retrievals`, `pending_feedback`, `feedback_gathering`, `validation_errors` | `last_retrievals["assembled"]` | Chroma, Neo4j, ArtifactStore | RetryPolicy(2) |
| `invoke_llm` | Agent Interaction | `last_retrievals["assembled"]` | `messages`, `reply_text`, `ready_for_production` | LLM (chat completion) + Presidio (output scan) | RetryPolicy(3) |
| `hitl_gate` | Agent Interaction | `brd_memory`, `pending_feedback` | `feedback_gathering`, `mode` | LangGraph `interrupt()` | — |
| `extract_memory` | Agent Interaction | `messages` (last 2 turns) | `brd_memory` | LangMem MemoryStoreManager | — |
| `draft_llm` | Drafting | `last_retrievals["assembled"]` | `last_retrievals["draft_raw"]` | Presidio (input scan) + LLM (JSON mode, 4k tokens) | RetryPolicy(3) |
| `schema_validate` | Drafting | `last_retrievals["draft_raw"]` | `current_draft`, `draft_status`, `draft_history`, `retry_count`, `validation_errors`, clears `pending_feedback` | Pydantic validation + Presidio (output scan) | — |
| `error_handler` | Drafting | `retry_count`, `validation_errors` | `reply_text` | — | — |

**Conditional Edges (v2):**

| Edge | After Node | Routing Function | Routes |
|---|---|---|---|
| `route_after_conversation` | `invoke_llm` | Inspects `ready_for_production` | `hitl_gate` \| `extract_memory` |
| `route_after_validation` | `schema_validate` | Inspects `current_draft`, `retry_count` | `END` \| `draft_llm` (retry) \| `error_handler` |

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

**Inference Priority (v2 — cascading classification):**
1. If `feedback_gathering == true` → `REFINEMENT` (or `BRD_UPDATE` if draft exists).
2. Explicit `mode` override (`drafting` → `BRD_GENERATION`; `request_changes` → `REFINEMENT`).
3. **Semantic router** (v2): Embedding-based intent classification using example utterances per route. Replaces keyword matching. Uses `semantic-router` library with local FastEmbed encoder (no API key required).
4. Default → `INFORMATION_GATHERING`.

---

## 6. Data Sources & Retrieval Flow

### 6.1 Vector Store (Chroma)
- **Content:** BRD templates, glossary entries, synthetic prior BRDs, **and ingested user documents**.
- **Embedding:** ONNX MiniLM-L6-v2 (local, no API key).
- **Usage:** Query by assembled context; return top-k document chunks.

### 6.2 Knowledge Graph / Enterprise KB (Neo4j)
- **Purpose:** Pre-seeded enterprise knowledge base. The agent **reads** enterprise context (stakeholders, business units, regulatory domains, systems) from the KG but does **not write** to it during the flow.
- **Schema:** Entities = `Stakeholder`, `BusinessUnit`, `RegulatoryDomain`, `System`. Relations = `WORKS_IN`, `OVERSEES`, `SUBJECT_TO`, `INTEGRATES_WITH`.
- **Query:** Token-based substring matching for entity linking → 1-hop traversal → return entities + neighbours.
- **Ingestion note:** Documents uploaded by the user are stored in the Vector DB only. Enriching the KG from ingested documents is a future option, not part of the current flow.

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
  └── InMemoryStore (volatile — but see safety net below)
       ↓
   BRDMemory items (extracted facts)
       ↓
Merged back into state.brd_memory
       ↓
state.brd_memory persisted by SqliteSaver (checkpointer)
       ↓
retrieve_context reads brd_memory from checkpointed state
       ↓
LLM uses accumulated facts to ask better questions / draft BRD
```

**Persistence Safety Net (v2):**
- LangMem uses `InMemoryStore` (no `SqliteStore` exists in LangGraph).
- However, `state.brd_memory` is persisted by `SqliteSaver` via the checkpointer.
- On process restart: InMemoryStore is empty, but `state.brd_memory` in the checkpoint has the last snapshot.
- Next `extract_memory` call starts fresh in the store but the prompt still has all accumulated facts.

**Feedback Gathering Extension:**
```
User Review Feedback → /request-changes
       ↓
   append to state.pending_feedback
       ↓
   feedback_gathering = true, draft_status = REJECTED
       ↓
   invoke_llm asks "Any more reviews?"
       ↓
   Loop until user says "no"
       ↓
   Conditional edge → hitl_gate (HITL 1 interrupt)
       ↓
   User confirms → /generate-brd
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
| `/chat` | POST | `{message, session_id?}` | `{reply, mode, draft?, turn_id, ready_for_production?, session_id}` | Agent Interaction |
| `/generate-brd` | POST | `{session_id}` | `{draft, mode, version}` | Drafting (atomic) |
| `/approve` | POST | `{session_id}` | `{status, brd_id, version}` | Set `draft_status = APPROVED` in state |
| `/request-changes` | POST | `{feedback, session_id}` | `{reply, draft, mode, feedback_gathering}` | Agent Interaction (feedback loop) |
| `/reset` | POST | — | `{status, session_id}` | New thread |
| `/memory` | GET | `session_id` | `{session_id, memory}` | Read state / LangMem |
| `/drafts` | GET | `session_id` | `{session_id, drafts: [{version, status, produced_at}]}` | Read `draft_history` from state |
| `/drafts/{version}` | GET | `session_id` | `{draft, provenance}` | Read specific version from `draft_history` |
| `/upload` | POST | `file, session_id` | `{job_id, status: "processing"}` | Background ingestion → Chroma |
| `/tools/fetch-brd-template` | POST | `{template_name?}` | `{artifact_ref, available_templates, note}` | ArtifactStore + transcript |

---

## 10. Key Design Rules

1. **🟡 Agent Interaction is the universal entry point** — initial prompts, clarifications, and review changes all enter here.
2. **🟢 Production is atomic** — BRD is produced/updated **once** per cycle with the complete gathered context. Multiple cycles are allowed (initial generation, then updates after feedback).
3. **Feedback re-entry is not a separate mode** — it is the same Agent Interaction loop, entered with the current draft in context. The user may attach supporting documents with feedback (ingested through the same pipeline), and the loop ends at the same HITL 1 gate.
4. **HITL gates are LangGraph interrupts** (Chassis §3.2.2) — HITL 1 uses `interrupt()` inside `hitl_gate` node. HITL 2 uses `interrupt_after=["schema_validate"]`. No ad-hoc branches. The agent cannot auto-jump to 🟢 Production; it must pause and present a summary and get explicit approval.
5. **🟢 → 🔵 is auto** — Once production starts, it completes and auto-delivers to Review Mode. No user action needed mid-production.
6. **Only 🔵 → 🟡 requires user action** — Requesting changes from Review Mode (HITL 2) is the only backward transition triggered by the user.
7. **The Knowledge Graph is a pre-seeded enterprise KB** — Ingestion populates the Vector DB only; the agent reads enterprise context from the KG but does not write to it. KG/VDB are direct retriever calls, not MCP (Chassis §3.3.1).
8. **Presidio guardrails wrap every LLM call** (Chassis §3.5) — Input: scan user messages and assembled context. Output: scan LLM replies and generated BRD. Financial/contact PII is masked; person names are logged but not masked.
9. **Every production cycle is versioned** — `draft_history` is append-only. Each entry captures the full provenance (prompt hash, strategy, model, token accounting) for audit trail.
10. **Rejection count is tracked, no hard cap** — `rejection_count` increments on each `POST /request-changes`. Logged for observability. Production environment will enforce the Chassis §3.8 escalation rule (3 rejections).
11. **Routing is via conditional edges, not implicit LLM logic** — `route_after_conversation` and `route_after_validation` are pure routing functions that inspect state. No side effects.

---

## 11. Resolved & Remaining Gaps

### ✅ Resolved in v2

1. ~~**No human-in-the-loop gate before drafting:**~~ → HITL 1 via LangGraph `interrupt()` in `hitl_gate` node.
2. ~~**No conditional edges in graphs:**~~ → `route_after_conversation` (gathering) and `route_after_validation` (drafting) replace linear chains.
3. ~~**Schema validation raises on failure:**~~ → Validate-Repair-Retry loop with bounded retries (max 2) and error feedback to LLM.
4. ~~**Context strategy inference is keyword-based:**~~ → Semantic router (embedding-based) replaces keyword matching.
5. ~~**Approval is a session flag only:**~~ → `draft_status` in `ChatbotState` + `draft_history` with full provenance. No more `SessionState`.
6. ~~**No guardrails/PII protection:**~~ → Presidio input/output wrappers on every LLM call (Chassis §3.5).
7. ~~**Global singleton session:**~~ → Request-scoped `session_id` with `SqliteSaver` persistence.
8. ~~**Volatile state (MemorySaver):**~~ → `SqliteSaver` checkpointer persists state across restarts.

### ⚠️ Remaining (Acceptable for PoC)

1. **Drafting graph does not use conversation memory extraction:** `draft_llm` and `schema_validate` are pure transform nodes; no memory is written during drafting.
2. **Rolling summary is lossy:** Trimmed messages are gone forever (only summary remains). No way to "unroll" if needed.
3. **Single-shot drafting:** No progressive / section-by-section draft generation. A 4k token limit could truncate complex BRDs.
4. **"Sufficient detail gathered?" is LLM-judged:** No hard requirements-coverage checklist before allowing transition to production.
5. **Memory dedup is naive:** Exact string match only. Two slightly different descriptions of the same stakeholder won't dedup.
6. **No document ingestion pipeline yet:** `/upload` endpoint designed but not implemented. Text-based documents only (per SOW §7).
7. **No cross-session learning:** Memory is session-scoped. No enterprise-level memory inheritance.

---

## Related Diagrams

- [Simplified BRD Agent Flow](diagrams/simplified_brd_agent_flow.md) — High-level 3-state flow with HITL gates.
- [Detailed Flow Diagram](diagrams/detailed_flow.md) — Data flow through every node and state field.
- [State Flow Diagram](diagrams/state_flow.md) — Mode transitions and state machine (3-state simplified).
- [Agent Interaction Graph Sequence](diagrams/gathering_graph.md) — Step-by-step node execution (includes feedback gathering loop).
- [Drafting Graph Sequence](diagrams/drafting_graph.md) — Step-by-step node execution (atomic production).
- [Context Assembly Flow](diagrams/context_assembly.md) — How the prompt is built.
- [Memory Lifecycle](diagrams/memory_lifecycle.md) — How memory is extracted, stored, and reused.
