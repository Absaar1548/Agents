# Phase 1 Plan — Session Management & Persistent Checkpointer

> **Goal:** Replace global singleton `SessionState` with request-scoped `session_id` and swap in-memory `MemorySaver` for persistent `SqliteSaver`. Wire `DraftStatus` into `ChatbotState`.
> **Depends on:** Phase 0 (completed)
> **Estimated effort:** 1 day

---

## 1. Summary of Changes

| # | File | Action |
|:---|:---|:---|
| 1 | `requirements.txt` | Add `langgraph-checkpoint-sqlite>=2.0` |
| 2 | `backend/core/graph.py` | Replace `MemorySaver()` with `SqliteSaver.from_conn_string(...)` |
| 3 | `backend/core/__init__.py` | Remove `checkpointer` from re-exports |
| 4 | `backend/session.py` | **Delete** — global singleton no longer needed |
| 5 | `backend/api/chat.py` | Accept `session_id` in request body; remove `get_session` / `reset_session` imports |
| 6 | `backend/api/drafting.py` | Accept `session_id` in request body; remove `get_session` import |
| 7 | `backend/api/review.py` | Accept `session_id` in request body; wire `draft_status` into graph state; remove `get_session` import |
| 8 | `backend/api/deps.py` | Update `_read_graph_state`, `_turn_id`, `_current_draft` signatures to take `thread_id` directly (already do) |
| 9 | `frontend/streamlit_app.py` | Generate `session_id` on init; send with every request; update `/reset` handling |
| 10 | `backend/main.py` | Ensure `storage/` directory exists at startup |

---

## 2. Detailed Design

### 2.1 Checkpointer Swap (`backend/core/graph.py`)

**Current:**
```python
from langgraph.checkpoint.memory import MemorySaver
checkpointer = MemorySaver()
```

**Target:**
```python
from langgraph.checkpoint.sqlite import SqliteSaver
checkpointer = SqliteSaver.from_conn_string("storage/checkpoints.db")
```

- The `storage/` directory already exists in the repo (gitignored) and will hold `checkpoints.db`.
- Both graphs (`gathering_graph` and `drafting_graph`) continue sharing the same checkpointer instance.
- `checkpointer` will be removed from `backend/core/__init__.py` re-exports because no external module references it.

### 2.2 Request-Scoped `session_id` — Backend

All three API modules (`chat.py`, `drafting.py`, `review.py`) currently call `get_session()` to obtain a global `SessionState`. We replace this with an incoming `session_id` field on every request body.

**Request model updates:**

```python
class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

class GenerateBRDRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

class ApproveRequest(BaseModel):
    session_id: str = Field(...)

class RequestChangesBody(BaseModel):
    feedback: str = Field(min_length=1)
    session_id: str = Field(...)

class ResetRequest(BaseModel):
    session_id: str = Field(...)
```

**Behavior:**
- If the client omits `session_id`, the backend generates a fresh UUID (backward-compatible for curl/manual testing).
- The `session_id` is used **verbatim** as the LangGraph `thread_id` in `_config(session_id)`.

### 2.3 `/reset` Endpoint Redesign

**Current behavior:** `reset_session()` rotates a global singleton → new thread_id → empty state.

**New behavior:**
1. Client sends current `session_id`.
2. Backend asks the checkpointer to delete that thread's checkpoints (clean slate).
3. Backend generates a **new** `session_id` and returns it.
4. Client replaces its local `session_id` with the new one.

```python
@router.post("/reset", response_model=ResetResponse)
def reset(body: ResetRequest, request: Request) -> ResetResponse:
    # Optional: purge old thread from SqliteSaver
    config = _config(body.session_id)
    try:
        request.app.state.gathering_graph.checkpointer.delete(config)
    except Exception:
        pass  # thread may not exist yet

    new_session_id = uuid.uuid4().hex
    return ResetResponse(status="ok", session_id=new_session_id)
```

> **Note:** `SqliteSaver` API for `delete()` will be verified during implementation. If unavailable, we simply return a new UUID and let the old thread data remain in SQLite (harmless for PoC).

### 2.4 Replacing `approved` Flag with `draft_status`

The `SessionState.approved` boolean is the only remaining piece of global state not captured in `ChatbotState`. We migrate it into the checkpointer-persisted state.

**`backend/api/review.py` changes:**

```python
# Approve endpoint
@router.post("/approve", response_model=ApproveResponse)
def approve(body: ApproveRequest, request: Request) -> ApproveResponse:
    draft = _current_draft(request, body.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to approve.")

    # Persist approval into graph state
    graph = request.app.state.gathering_graph
    config = _config(body.session_id)
    graph.update_state(config, {"draft_status": DraftStatus.APPROVED})

    # ... telemetry ...
    return ApproveResponse(status="approved", brd_id=str(draft.brd_id))
```

```python
# Request-changes endpoint
@router.post("/request-changes", response_model=RequestChangesResponse)
def request_changes(body: RequestChangesBody, request: Request) -> RequestChangesResponse:
    draft = _current_draft(request, body.session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No draft to revise.")

    # Mark as rejected + increment rejection_count
    graph = request.app.state.gathering_graph
    config = _config(body.session_id)
    state = _read_graph_state(request, body.session_id)
    rejection_count = state.get("rejection_count", 0) + 1
    graph.update_state(config, {
        "draft_status": DraftStatus.REJECTED,
        "rejection_count": rejection_count,
    })

    # ... rest of graph invocation ...
```

**`backend/api/chat.py` — mode decision logic:**

```python
state = _read_graph_state(request, body.session_id)
existing_draft = _current_draft(request, body.session_id)
draft_status = state.get("draft_status")

if existing_draft is not None and draft_status != DraftStatus.APPROVED:
    mode = "request_changes"
    ...
else:
    mode = "gathering"
    ...
```

### 2.5 Frontend Changes (`frontend/streamlit_app.py`)

**Init:**
```python
def init_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "current_draft" not in st.session_state:
        st.session_state.current_draft = None
    if "approved_brd_id" not in st.session_state:
        st.session_state.approved_brd_id = None
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex
```

**API helpers updated to pass `session_id`:**
```python
def api_chat(message: str, session_id: str) -> dict:
    with _client() as c:
        r = c.post("/chat", json={"message": message, "session_id": session_id})
        ...

def api_reset(session_id: str) -> dict:
    with _client() as c:
        r = c.post("/reset", json={"session_id": session_id})
        ...
```

**Reset button behavior:**
```python
if st.button("Reset session", ...):
    res = api_reset(st.session_state.session_id)
    st.session_state.session_id = res["session_id"]
    st.session_state.messages = []
    st.session_state.current_draft = None
    st.session_state.approved_brd_id = None
    st.rerun()
```

**All other API calls updated** to pass `st.session_state.session_id`.

### 2.6 `backend/api/deps.py` — No Signature Change Required

The helpers `_config`, `_read_graph_state`, `_turn_id`, `_current_draft` already accept `thread_id: str` directly. The API endpoints will simply pass `body.session_id` instead of `session.session_id`. No changes needed in `deps.py`.

### 2.7 `backend/main.py` — Ensure Storage Directory

Add at the top of lifespan:
```python
from pathlib import Path
Path("storage").mkdir(exist_ok=True)
```

This ensures `storage/checkpoints.db` can be created even on a fresh clone.

---

## 3. Files Changed — Line-by-line Preview

### `requirements.txt`
```diff
  langgraph>=0.2
+ langgraph-checkpoint-sqlite>=2.0
```

### `backend/core/graph.py`
```diff
- from langgraph.checkpoint.memory import MemorySaver
- checkpointer = MemorySaver()
+ from langgraph.checkpoint.sqlite import SqliteSaver
+ checkpointer = SqliteSaver.from_conn_string("storage/checkpoints.db")
```

### `backend/core/__init__.py`
```diff
- from backend.core.graph import AgentRuntime, build_drafting_graph, build_gathering_graph, checkpointer
+ from backend.core.graph import AgentRuntime, build_drafting_graph, build_gathering_graph
```

### `backend/api/chat.py`
- Remove `from backend.session import get_session, reset_session`
- Add `import uuid`
- Update `ChatRequest` with `session_id` field
- Update `ResetRequest` with `session_id` field
- Update `/chat` to use `body.session_id`
- Update `/reset` to accept `body.session_id`, optionally delete old thread, return new UUID
- Update `/memory` to use `body.session_id` (or query param for GET)
- Update `/tools/fetch-brd-template` to use `body.session_id`
- Replace `session.approved` check with `draft_status != DraftStatus.APPROVED`

### `backend/api/drafting.py`
- Remove `from backend.session import get_session`
- Update `/generate-brd` to accept `session_id` in request body

### `backend/api/review.py`
- Remove `from backend.session import get_session`
- Add `from backend.core.state import DraftStatus`
- Update `/approve` to persist `draft_status=APPROVED` into graph state
- Update `/request-changes` to persist `draft_status=REJECTED` and increment `rejection_count`
- Both endpoints accept `session_id` in request body

### `frontend/streamlit_app.py`
- Add `import uuid`
- Add `session_id` to `init_state()`
- Update all `api_*` helpers to accept and send `session_id`
- Update reset handler to receive and store new `session_id`

---

## 4. Verification Steps

1. **Install new dependency:** `pip install langgraph-checkpoint-sqlite>=2.0`
2. **Start backend:** `uvicorn backend.main:app --reload --port 8000`
3. **Open two browser tabs** with Streamlit frontend:
   - Send different messages in each tab
   - Verify each tab maintains its own conversation history (independent `session_id`)
4. **Kill and restart the server**
   - Refresh each tab
   - Verify conversation history **persists** ( SqliteSaver test )
5. **Reset test:**
   - Click "Reset session" in one tab
   - Verify that tab starts fresh while the other tab's history remains intact
6. **Approval flow test:**
   - Generate BRD → Approve → verify `draft_status=APPROVED` prevents mode flip to `request_changes` on next chat
7. **Import sweep:** `python -c "from backend.main import app; print('OK')"`

---

## 5. Exit Criteria

- [ ] `backend/session.py` is deleted and no module imports it.
- [ ] `MemorySaver` is replaced by `SqliteSaver` pointing to `storage/checkpoints.db`.
- [ ] Every API endpoint accepts a request-scoped `session_id` used as LangGraph `thread_id`.
- [ ] Frontend generates, stores, and sends `session_id` on every request.
- [ ] Two browser tabs can run **independent sessions** simultaneously.
- [ ] Killing and restarting the FastAPI server **resumes** existing conversations.
- [ ] `draft_status` enum is wired into `/approve` and `/request-changes` endpoints.
- [ ] App starts without import errors.

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|:---|:---|
| `SqliteSaver` delete API may differ or not exist | Try/catch around delete; old data in SQLite is harmless for PoC |
| Frontend generates new session_id before backend confirms reset | Backend always returns a new UUID on `/reset`; frontend uses returned value |
| Streamlit re-runs cause duplicate `session_id` generation | Only generate if `"session_id" not in st.session_state` |
| `langgraph-checkpoint-sqlite` version conflict | Pin `>=2.0,<4.0` to match `langgraph-checkpoint` major version |

---

## 7. Discrepancy Notes (to be filled during execution)

> Any deviations from this plan discovered during implementation will be logged here and then copied to `discrepancy_notes.md`.
