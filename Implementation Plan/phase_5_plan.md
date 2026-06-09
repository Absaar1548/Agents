# Phase 5 Plan — Enhancements

> **Goal:** Four independent enhancements: semantic router, document ingestion, retry/timeouts, and agent manifest.
> **Depends on:** Phase 2 (graph topology)
> **Est. effort:** 3–4 days

---

## Sub-Task Overview

| # | Sub-Task | Key Files |
|:---|:---|:---|
| 5A | Semantic Router | `backend/context/strategy.py`, `requirements.txt` |
| 5B | Document Ingestion | `backend/ingestion/parser.py`, `backend/api/upload.py`, `requirements.txt` |
| 5C | RetryPolicy & Timeouts | `backend/core/graph.py`, `backend/llm.py` |
| 5D | Agent Manifest | `backend/core/manifest.yaml`, `backend/main.py`, `backend/telemetry.py` |

Each sub-task is independent and verifiable on its own.

---

## 5A: Semantic Router

### Current State
`backend/context/strategy.py` uses hardcoded keyword sets (`_BRD_GEN_KEYWORDS`, `_REFINE_KEYWORDS`, etc.) with substring matching via `any(kw in m for kw in ...)`. This is brittle — minor phrasing variations miss the target strategy.

### Target
Replace keyword matching with `semantic-router` (embedding-based classification) while preserving the mode-override priority: **mode override → semantic router → default**.

### Implementation

1. Add `semantic-router>=0.1` to `requirements.txt` (it pulls its own fast embedder — `fastembed` or uses Chroma's ONNX model).
2. Define `Route` objects, one per inferrable strategy:
   - `BRD_GENERATION` — 5-6 utterances (e.g. "draft the BRD", "generate the document", "create the BRD", "make a BRD", "produce the requirements document")
   - `REFINEMENT` — 5-6 utterances (e.g. "change the scope section", "modify FR-001", "revise the assumptions", "update stakeholder list", "edit the risks")
   - `REVIEW_FEEDBACK` — 4 utterances (e.g. "review the draft", "check the BRD", "validate the requirements", "look at the document")
   - `REQUIREMENT_CONSOLIDATION` — 4 utterances (e.g. "organise what we have", "summarize the requirements", "consolidate what we discussed", "structure the information")
   - `TOOL_REASONING` — 3 utterances (e.g. "find the template", "search for", "look up the glossary")
3. Initialize `SemanticRouter` as a module-level singleton (lazy-load pattern, like the Presidio engines).
4. `infer_strategy()`:
   - Mode override logic unchanged (lines 61-64 of strategy.py)
   - Replace keyword blocks with a single `router()` call when `user_message` is present
   - Fallback to `INFORMATION_GATHERING` if below confidence threshold or on error

### Risk Mitigation
Per master plan Risk Register: use `fastembed` (small ONNX model, already used by Chroma). The `semantic-router` package ships with fastembed; no external API needed.

---

## 5B: Document Ingestion Pipeline

### Current State
- `backend/ingestion/parser.py` is a stub (`parse_and_chunk` returns `[]`).
- `backend/api/upload.py` is a stub (returns 501 not implemented).
- `backend/sources/vector_store.py` (`ChromaRetrievalSource`) has an `add()` method ready for ingestion — no write-path changes needed.

### Target
Accept file uploads (PDF, TXT), extract text, chunk, and store in the existing Chroma collection with session-scoped metadata.

### Implementation

1. Add `pymupdf>=1.23` to `requirements.txt` (lightweight, no system deps).
2. Implement `backend/ingestion/parser.py`:
   - `extract_text(file_path: str, mime_type: str) -> str`:
     - `.txt` → read directly
     - `.pdf` → `fitz.open()` → iterate pages, join with newlines
   - `chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]`:
     - Simple character-split with overlap (no LangChain `RecursiveCharacterTextSplitter` to avoid extra dep — plain Python is sufficient)
3. Implement `backend/api/upload.py`:
   - `POST /upload` accepts `UploadFile` + `session_id: str`
   - Validates MIME type (`text/plain` or `application/pdf`)
   - Saves to temp file, extracts text, chunks, stores each chunk in Chroma via `ChromaRetrievalSource.add()`
   - Returns `{status: "ingested", chunks: N, session_id: str}`
   - Per SOW §7: **text-only — no image/diagram parsing**
4. Wire: `upload.py` needs access to `ChromaRetrievalSource` — pass it via `app.state.docs_source` (already created in `main.py` lifespan).

### Files to modify/create
- `backend/ingestion/parser.py` — full rewrite (currently stub)
- `backend/api/upload.py` — full rewrite (currently stub)
- `requirements.txt` — add `pymupdf`
- `backend/api/deps.py` — add helper for `ChromaRetrievalSource` access

---

## 5C: RetryPolicy & Timeouts

### Current State
- LLM calls in `summarize.py`, `invoke_llm.py`, `draft_llm.py` have no retry logic — a single transient failure propagates up.
- `BaseLLMClient.complete()` has no `timeout` parameter — `httpx` default (~60s for OpenAI SDK) handles it implicitly.
- The OpenAI SDK's `Timeout` class supports explicit `connect` / `read` / `write` timeouts.

### Target
Add LangGraph `RetryPolicy` to LLM-calling nodes so transient failures retry automatically. Add explicit `timeout` to the LLM client.

### Implementation

1. **Add explicit timeout to `BaseLLMClient.complete()`**:
   - Accept `timeout: Optional[float] = None` parameter
   - `AzureOpenAIClient`: pass `timeout=timeout` to `self._client.chat.completions.create()` — the OpenAI SDK accepts `timeout` in seconds or a `Timeout` object
   - `OllamaClient`: same
   - Callers (`invoke_llm.py`, `draft.py`, `summarize.py`): pass `timeout=60.0`
   - Default `timeout=60.0` on the `complete()` method signature for safety

2. **Add `RetryPolicy` to LLM-calling nodes in graph compilation**:
   ```python
   from langgraph.pregel import RetryPolicy
   
   llm_retry = RetryPolicy(
       max_attempts=3,
       initial_interval=1.0,
       backoff_factor=2.0,
   )
   
   non_llm_retry = RetryPolicy(
       max_attempts=2,
       initial_interval=0.5,
       backoff_factor=2.0,
   )
   ```
   - `summarize` → `retry=llm_retry`
   - `invoke_llm` → `retry=llm_retry`
   - `draft_llm` → `retry=llm_retry`
   - `retrieve_context` → `retry=non_llm_retry` (calls KG + Chroma, can fail transiently)
   - `extract_memory` → `retry=non_llm_retry`
   - Other nodes (`hitl_gate`, `schema_validate`, `error_handler`): no retry needed

3. **Update graph builders** in `backend/core/graph.py`:
   - `add_node("summarize", ..., retry=llm_retry)`
   - `add_node("invoke_llm", ..., retry=llm_retry)`
   - `add_node("draft_llm", ..., retry=llm_retry)`
   - `add_node("retrieve_context", ..., retry=non_llm_retry)`

---

## 5D: Agent Manifest

### Current State
Agent identity is hardcoded as constants in `backend/telemetry.py` (`AGENT_ID = "brd-agent"`, `SERVICE_VERSION = "0.1.0"`). The `/health` endpoint returns `{"ok": True, "agent_id": "brd-agent", "version": "0.1.0"}`. No structured manifest file exists.

### Target
Create a YAML manifest file that defines the agent's identity, capabilities, tools, HITL gates, and guardrail profile. Load at startup and expose via `/health`.

### Implementation

1. Create `backend/core/manifest.yaml`:
   ```yaml
   agent:
     id: brd-agent
     version: "0.1.0"
     description: "Conversational BRD authoring agent for NT financial services"
   
   capabilities:
     modes: [gathering, drafting, request_changes]
     memory: [typed_facts, rolling_summary]
     context_sources: [vector_store, knowledge_graph]
   
   hitl_gates:
     - name: hitl_1
       description: "Confirm readiness before BRD production"
       trigger: ready_for_production
     - name: hitl_2
       description: "Review and approve/reject generated BRD"
       trigger: draft_status == DRAFT
   
   guardrails:
     presidio:
       mask_entities: [CREDIT_CARD, US_SSN, EMAIL_ADDRESS, PHONE_NUMBER, US_BANK_NUMBER, IBAN_CODE]
       log_only_entities: [PERSON, IP_ADDRESS]
   
   observability:
     service_name: brd-agent
     service_namespace: brd-poc
   
   checkpointer:
     type: sqlite
     path: storage/checkpoints.db
   
   embedding:
     provider: chroma_default
     model: all-MiniLM-L6-v2
   ```

2. Load manifest in `backend/main.py` lifespan:
   ```python
   import yaml
   with open(Path(__file__).parent / "core" / "manifest.yaml") as f:
       manifest = yaml.safe_load(f)
   app.state.manifest = manifest
   ```

3. Update `/health` endpoint to include manifest:
   ```python
   @app.get("/health")
   def health(request: Request) -> dict:
       manifest = request.app.state.manifest
       return {
           "ok": True,
           "agent_id": manifest["agent"]["id"],
           "version": manifest["agent"]["version"],
           "capabilities": manifest["capabilities"],
           "hitl_gates": manifest["hitl_gates"],
           "guardrails": manifest["guardrails"],
       }
   ```

4. Optionally load telemetry constants (`AGENT_ID`, `SERVICE_VERSION`) from manifest instead of hardcoding — but keep backward compatibility by falling back to the constants if manifest is missing.

5. Add `pyyaml>=6.0` to `requirements.txt` (FastAPI already pulls this in as a transitive dep, but pin explicitly).

---

## 5. Testing Strategy

### 5A: Semantic Router
- Unit test: `infer_strategy()` routes common phrases to correct strategies
- Verify cascading: mode=drafting overrides all keywords
- Verify default: unknown → INFORMATION_GATHERING

### 5B: Document Ingestion
- Unit test: `extract_text()` with `.txt` and `.pdf` sample files
- Unit test: `chunk_text()` produces correct number of chunks with overlap
- Integration test: `POST /upload` stores chunks in Chroma, retrievable via query

### 5C: RetryPolicy & Timeouts
- Verify graph compiles with `retry=` kwargs (import check)
- Unit test: mock `complete()` to fail twice then succeed; retry policy should recover

### 5D: Agent Manifest
- Verify YAML loads at startup
- `/health` returns manifest fields
- Verify app starts without manifest (fallback to constants)

---

## 6. Implementation Order

Since these are independent, build in order of testability and impact:

1. **5D** — Agent Manifest (trivial, adds structure)
2. **5C** — RetryPolicy & Timeouts (small change, high reliability impact)
3. **5A** — Semantic Router (medium complexity, requires model download)
4. **5B** — Document Ingestion (most complex, needs file handling + Chroma write)

---

## 7. Exit Criteria

- [ ] Semantic router classifies user messages into correct strategies, with mode override preserved
- [ ] `POST /upload` accepts PDF/TXT files, extracts text, chunks, stores in Chroma
- [ ] App survives transient LLM failures with automatic retry + backoff
- [ ] `/health` returns full manifest with capabilities, guardrails, HITL gates
- [ ] All unit tests pass (existing + new)
- [ ] App imports and starts cleanly
