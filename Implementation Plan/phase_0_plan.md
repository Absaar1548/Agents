# Phase 0 — Project Restructure & Foundation

> **Goal:** Reorganize the codebase into the target folder structure so all subsequent phases have a clean base to work on. **No behavior changes.**
>
> **Scope:** Pure refactor — move files, split `main.py`, create stub packages, update imports, add `tests/` skeleton.
>
> **Exit Criteria:** App runs identically to current state but with new folder structure. Smoke tests pass.

---

## 1. Current State Snapshot

The codebase has 24 `.py` files under `backend/` with a flat structure. `main.py` is a 474-line monolith. No `tests/` directory exists.

**Import graph (simplified):**
- `main.py` imports from 10+ modules
- All 6 node files import `AgentRuntime` + `ChatbotState` from `backend.graph`
- `graph.py` defines `ChatbotState`, `ChatMode`, `AgentRuntime`, `checkpointer`, and both graph builders
- `context_assembler.py` imports `prompts`, `strategy`, `sources.base`, `telemetry`
- `memory.py` is imported by `main.py` and `nodes/extract_memory.py`

---

## 2. Target Folder Structure

```
backend/
├── __init__.py
├── main.py                    # Thin router + lifespan + /health
├── api/                       # FastAPI endpoint modules (split from main.py)
│   ├── __init__.py
│   ├── deps.py                # Shared helpers: _config, _read_graph_state, _turn_id, _current_draft, get_agent_runtime
│   ├── chat.py                # /chat, /reset, /memory, /tools/fetch-brd-template
│   ├── drafting.py            # /generate-brd
│   ├── review.py              # /approve, /request-changes
│   └── upload.py              # Stub /upload (Phase 5)
├── core/                      # Core domain logic
│   ├── __init__.py
│   ├── graph.py               # From backend/graph.py (minus state definitions)
│   ├── schema.py              # From backend/schema.py
│   ├── state.py               # ChatbotState, ChatMode, DraftStatus, new stub fields
│   └── manifest.yaml          # Stub agent manifest (Phase 5)
├── memory/                    # Memory management
│   ├── __init__.py            # Re-exports from manager.py
│   └── manager.py             # From backend/memory.py
├── context/                   # Context assembly
│   ├── __init__.py            # Re-exports
│   ├── assembler.py           # From backend/context_assembler.py
│   ├── strategy.py            # From backend/strategy.py
│   └── prompts.py             # From backend/prompts.py
├── guardrails/                # Presidio integration (Phase 3)
│   ├── __init__.py
│   └── presidio.py            # Stub
├── ingestion/                 # Document ingestion pipeline (Phase 5)
│   ├── __init__.py
│   └── parser.py              # Stub
├── nodes/                     # Graph nodes (existing, import updates only)
│   ├── __init__.py
│   ├── summarize.py           # Import update: prompts → context.prompts
│   ├── retrieve_context.py    # Import update: strategy → context.strategy
│   ├── invoke_llm.py          # Import update: graph → core.graph + core.state
│   ├── extract_memory.py      # Import update: graph → core.graph + core.state; memory → memory.manager
│   ├── draft.py               # Import update: graph → core.graph + core.state
│   ├── schema_validate.py     # Import update: graph → core.graph + core.state; schema → core.schema
│   ├── hitl_gate.py           # Stub (Phase 2)
│   └── error_handler.py       # Stub (Phase 2)
├── sources/                   # Retrieval sources (unchanged)
│   ├── __init__.py
│   ├── base.py
│   ├── vector_store.py
│   └── kg.py
├── tools/                     # Agent tools (unchanged)
│   ├── __init__.py
│   └── fetch_brd_template.py
├── telemetry.py               # Unchanged
├── llm.py                     # Unchanged
└── artifacts.py               # Unchanged

tests/
├── conftest.py
├── unit/
│   └── __init__.py
└── integration/
    └── __init__.py
```

---

## 3. File-by-File Changes

### 3.1 New files to create

| File | Content |
|:---|:---|
| `backend/api/__init__.py` | Empty or router re-exports |
| `backend/api/deps.py` | `_config()`, `_read_graph_state()`, `_turn_id()`, `_current_draft()`, `get_agent_runtime()`, `get_gathering_graph()`, `get_drafting_graph()` — all using `request.app.state` |
| `backend/api/chat.py` | `/chat`, `/reset`, `/memory`, `/tools/fetch-brd-template` endpoints + their Pydantic models |
| `backend/api/drafting.py` | `/generate-brd` endpoint + `DraftResponse` model |
| `backend/api/review.py` | `/approve`, `/request-changes` endpoints + their models |
| `backend/api/upload.py` | Stub `/upload` returning 501 |
| `backend/core/__init__.py` | Re-exports: `AgentRuntime`, `build_gathering_graph`, `build_drafting_graph`, `checkpointer` |
| `backend/core/state.py` | `ChatMode`, `ChatbotState` (from graph.py), `DraftStatus` enum, stub new fields (`ready_for_production`, `retry_count`, `validation_errors`, `draft_status`, `rejection_count`, `draft_history`) |
| `backend/core/manifest.yaml` | Stub manifest per Chassis §4 |
| `backend/memory/__init__.py` | Re-exports from `manager.py` |
| `backend/context/__init__.py` | Re-exports from `assembler`, `strategy`, `prompts` |
| `backend/guardrails/__init__.py` | Empty |
| `backend/guardrails/presidio.py` | Stub with `scan_and_protect` placeholder |
| `backend/ingestion/__init__.py` | Empty |
| `backend/ingestion/parser.py` | Stub with `parse_and_chunk` placeholder |
| `backend/nodes/hitl_gate.py` | Stub node (Phase 2) |
| `backend/nodes/error_handler.py` | Stub node (Phase 2) |
| `tests/conftest.py` | Empty pytest conftest |
| `tests/unit/__init__.py` | Empty |
| `tests/integration/__init__.py` | Empty |

### 3.2 Files to move (with content + import updates)

| From | To | Import changes needed |
|:---|:---|:---|
| `backend/graph.py` | `backend/core/graph.py` | Remove `ChatbotState`, `ChatMode` definitions; import them from `backend.core.state`. Update import: `backend.context_assembler` → `backend.context.assembler` |
| `backend/schema.py` | `backend/core/schema.py` | None — self-contained |
| `backend/memory.py` | `backend/memory/manager.py` | None — imports `backend.llm` which stays |
| `backend/context_assembler.py` | `backend/context/assembler.py` | `backend.prompts` → `backend.context.prompts`; `backend.strategy` → `backend.context.strategy`; `backend.sources.base` stays; `backend.telemetry` stays |
| `backend/strategy.py` | `backend/context/strategy.py` | None — self-contained |
| `backend/prompts.py` | `backend/context/prompts.py` | None — self-contained |

### 3.3 Files to update in-place (import changes only)

| File | Import updates |
|:---|:---|
| `backend/nodes/summarize.py` | `backend.graph` → `backend.core.graph` + `backend.core.state`; `backend.prompts` → `backend.context.prompts` |
| `backend/nodes/retrieve_context.py` | `backend.graph` → `backend.core.graph` + `backend.core.state`; `backend.strategy` → `backend.context.strategy` |
| `backend/nodes/invoke_llm.py` | `backend.graph` → `backend.core.graph` + `backend.core.state` |
| `backend/nodes/extract_memory.py` | `backend.graph` → `backend.core.graph` + `backend.core.state`; `backend.memory` → `backend.memory.manager` |
| `backend/nodes/draft.py` | `backend.graph` → `backend.core.graph` + `backend.core.state` |
| `backend/nodes/schema_validate.py` | `backend.graph` → `backend.core.graph` + `backend.core.state`; `backend.schema` → `backend.core.schema` |

### 3.4 `backend/main.py` — full rewrite (thin router)

Current `main.py` (474 lines) becomes:
- Lifespan: builds runtime, sources, assembler, graphs — stores on `app.state`
- `/health` endpoint
- Router imports and mounting from `api/chat.py`, `api/drafting.py`, `api/review.py`, `api/upload.py`

All Pydantic request/response models move to their respective `api/*.py` modules.

---

## 4. Key Design Decisions

### 4.1 Shared helpers via `api/deps.py` + `app.state`

Instead of module-level globals (`_gathering_graph`, `_runtime`), store them on `app.state` in lifespan. `api/deps.py` provides dependency functions that extract them from `request.app.state`. This is FastAPI-idiomatic and eliminates global state in API modules.

```python
# backend/api/deps.py
from fastapi import Request
from backend.core.graph import AgentRuntime

def get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.runtime

def get_gathering_graph(request: Request):
    return request.app.state.gathering_graph

def get_drafting_graph(request: Request):
    return request.app.state.drafting_graph

def _config(request: Request, thread_id: str) -> dict: ...
def _read_graph_state(request: Request, thread_id: str) -> dict: ...
def _turn_id(request: Request, thread_id: str) -> int: ...
def _current_draft(request: Request, thread_id: str) -> Optional[BRDResponse]: ...
```

### 4.2 `__init__.py` re-exports

New packages use `__init__.py` re-exports for convenience. This means fewer import changes in consumer files. Example:

```python
# backend/memory/__init__.py
from backend.memory.manager import (
    BRDMemory, ConversationSummary,
    build_memory_manager, manager_config,
    read_memory, read_summary, memory_summary,
)
```

This lets `main.py` keep `from backend.memory import build_memory_manager, read_memory` without changes.

### 4.3 Stub fields in `ChatbotState`

`core/state.py` adds new fields as TypedDict keys (all optional since `total=False`). They are **not wired** — no node reads or writes them. This is safe because LangGraph ignores unset optional TypedDict keys.

```python
class ChatbotState(TypedDict, total=False):
    # ... existing fields ...
    ready_for_production: bool      # Phase 2
    retry_count: int                # Phase 2
    validation_errors: list[str]    # Phase 2
    draft_status: DraftStatus       # Phase 2
    rejection_count: int            # Phase 2
    draft_history: list[dict]       # Phase 4
```

### 4.4 No import changes for untouched files

These files stay exactly as-is:
- `backend/telemetry.py`
- `backend/llm.py`
- `backend/artifacts.py`
- `backend/sources/base.py`
- `backend/sources/vector_store.py`
- `backend/sources/kg.py`
- `backend/tools/fetch_brd_template.py`
- `frontend/streamlit_app.py`

---

## 5. Risk Mitigation

| Risk | Mitigation |
|:---|:---|
| Import cycle between `core/graph.py` and `core/state.py` | `state.py` defines only types/enums — no imports from `core/` |
| LangGraph `StateGraph(ChatbotState)` breaks after move | `ChatbotState` is the same class object, just imported from a different module |
| `__init__.py` re-exports mask import errors | Run `python -c "import backend.main"` after each batch of moves |
| `main.py` lifespan loses access to runtime | Store on `app.state` — all API modules access via `request.app.state` |
| FastAPI router mounting breaks paths | Prefixes are empty (`""`) since full paths are in `@app.post` decorators |

---

## 6. Verification Plan

1. **Import sanity:** `python -c "import backend.main; print('OK')"`
2. **Server starts:** `uvicorn backend.main:app --reload --port 8000` (5 seconds, no errors)
3. **Health check:** `curl http://localhost:8000/health` returns `{"ok": true, ...}`
4. **Smoke tests:** Run existing smoke scripts:
   - `python scripts/smoke_llm.py`
   - `python scripts/smoke_chat.py`
   - `python scripts/smoke_memory.py`
5. **Frontend connects:** Streamlit loads and shows "connected"
6. **Git diff review:** Confirm only `backend/` and `tests/` changed; no changes to `frontend/`, `scripts/`, `docs/`

---

## 7. Implementation Order

```
Step 1: Create all new directories + __init__.py files + stub files
Step 2: Move domain files (graph→core, schema→core, memory→memory/, assembler→context/, strategy→context/, prompts→context/)
Step 3: Update imports in moved files
Step 4: Create core/state.py with extracted state definitions + stubs
Step 5: Create api/deps.py with shared helpers
Step 6: Split main.py into api/chat.py, api/drafting.py, api/review.py, api/upload.py
Step 7: Rewrite main.py as thin router
Step 8: Update imports in all node files
Step 9: Create tests/ directory structure
Step 10: Verify (import sanity → server start → health → smoke tests)
```
