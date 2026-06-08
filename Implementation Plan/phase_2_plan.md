# Phase 2 Plan — Graph Topology (Conditional Edges + HITL Interrupts)

> **Goal:** Replace linear chains with conditional edges. Wire HITL 1 as LangGraph `interrupt()` in `hitl_gate`. Wire HITL 2 as `interrupt_after` on `schema_validate`. Add validation retry loop (max 2) in drafting graph.
> **Depends on:** Phase 1 (completed)
> **Estimated effort:** 2–3 days
> **Risk:** High — this changes core graph topology and endpoint behavior.

---

## 1. Summary of Changes

| # | File | Action |
|:---|:---|:---|
| 1 | `backend/core/state.py` | Add `feedback_gathering` (bool), `pending_feedback` (list[str]) fields |
| 2 | `backend/nodes/invoke_llm.py` | Detect `[READY_FOR_PRODUCTION]` marker in LLM reply; set `ready_for_production = True`; strip marker from reply |
| 3 | `backend/nodes/hitl_gate.py` | Implement `interrupt()` with requirements summary; handle resume value |
| 4 | `backend/nodes/schema_validate.py` | Stop raising on failure; set `retry_count` + `validation_errors`; append to `draft_history` on success |
| 5 | `backend/nodes/error_handler.py` | Return `reply_text` with validation error summary |
| 6 | `backend/nodes/retrieve_context.py` | Pass `validation_errors` to `ContextAssembler.assemble()` |
| 7 | `backend/context/assembler.py` | Add `validation_errors` parameter; inject error block into prompt for retry |
| 8 | `backend/core/graph.py` | Add `route_after_conversation` conditional edge; add `route_after_validation` conditional edge; add `interrupt_after=["schema_validate"]` to drafting graph |
| 9 | `backend/api/chat.py` | Catch `GraphInterrupt` from HITL 1; return `status: "hitl_1"`; add `POST /resume` endpoint for interrupt resume |
| 10 | `backend/api/drafting.py` | Handle `GraphInterrupt` from HITL 2 (interrupt_after) |
| 11 | `frontend/streamlit_app.py` | HITL 1 UI: show summary + "Proceed" / "Add More" buttons |
| 12 | `backend/context/prompts.py` | Update gathering system prompt to include `[READY_FOR_PRODUCTION]` marker instruction |

---

## 2. Detailed Design

### 2.1 State Fields (`backend/core/state.py`)

Add two new fields for the HITL 1 feedback sub-loop:

```python
class ChatbotState(TypedDict, total=False):
    # ... existing fields ...
    ready_for_production: bool
    retry_count: int
    validation_errors: list[str]
    draft_status: DraftStatus
    rejection_count: int
    draft_history: list[dict]
    # NEW Phase 2 fields
    feedback_gathering: bool      # True when user chose "Add more" at HITL 1
    pending_feedback: list[str]   # Accumulated feedback items for next production cycle
```

### 2.2 invoke_llm — Ready Detection (`backend/nodes/invoke_llm.py`)

**Change:** After the LLM replies, check for a `[READY_FOR_PRODUCTION]` marker.

```python
READY_MARKER = "[READY_FOR_PRODUCTION]"

reply = runtime.llm.complete(...)
ready = READY_MARKER in reply
clean_reply = reply.replace(READY_MARKER, "").strip()

return {
    "messages": [AIMessage(content=clean_reply)],
    "reply_text": clean_reply,
    "ready_for_production": ready,
}
```

The **gathering system prompt** must be updated to instruct the LLM:
> "When you believe you have gathered enough information to produce a complete BRD, include `[READY_FOR_PRODUCTION]` at the end of your response. Otherwise, continue asking clarifying questions."

**Fallback heuristic:** If the LLM hasn't emitted the marker after 8 turns, `invoke_llm` can auto-set `ready_for_production = True` to avoid infinite gathering (PoC pragmatism — configurable threshold).

### 2.3 hitl_gate — HITL 1 Interrupt (`backend/nodes/hitl_gate.py`)

```python
from langgraph.types import interrupt

def hitl_gate(state, config, runtime):
    brd_memory = state.get("brd_memory") or {}
    pending_feedback = state.get("pending_feedback") or []
    session_id = config["configurable"]["thread_id"]

    # Build a concise summary from brd_memory
    summary = {
        "title": brd_memory.get("title", "Untitled"),
        "objectives": brd_memory.get("objectives", []),
        "open_questions": brd_memory.get("open_questions", []),
        "feedback_items": len(pending_feedback),
    }

    # LangGraph interrupt — pauses graph, persists state to SqliteSaver
    action = interrupt({
        "type": "hitl_1",
        "summary": summary,
        "actions": ["proceed", "add_more"],
    })

    # action is the resume value from Command(resume=...)
    if action == "proceed":
        return {"mode": "drafting", "feedback_gathering": False}
    else:  # "add_more"
        return {
            "mode": "gathering",
            "feedback_gathering": True,
            "ready_for_production": False,
        }
```

### 2.4 Gathering Graph Topology (`backend/core/graph.py`)

```python
def route_after_conversation(state: ChatbotState) -> str:
    if state.get("ready_for_production"):
        return "hitl_gate"
    return "extract_memory"

def build_gathering_graph(runtime, checkpointer):
    # ... existing nodes ...
    from backend.nodes.hitl_gate import hitl_gate
    g.add_node("hitl_gate", _bind(hitl_gate, runtime))
    # existing edges
    g.add_conditional_edges(
        "invoke_llm",
        route_after_conversation,
        {"hitl_gate": "hitl_gate", "extract_memory": "extract_memory"},
    )
    g.add_edge("hitl_gate", END)
    # extract_memory already goes to END
    return g.compile(checkpointer=checkpointer)
```

### 2.5 schema_validate — No-Raise Retry (`backend/nodes/schema_validate.py`)

Replace the `raise` with state updates:

```python
try:
    candidate = _extract_json(raw)
    data = json.loads(candidate)
    data["drafted_by"] = DRAFTED_BY
    brd = BRDResponse.model_validate(data)
    # Success path
    span.set_attribute("schema.outcome", "valid")
    # ... span attrs ...
    return {
        "current_draft": brd.model_dump(mode="json"),
        "draft_status": DraftStatus.DRAFT,
        "retry_count": 0,
        "validation_errors": [],
        # Phase 4 will append to draft_history here
    }
except Exception as exc:
    span.set_attribute("schema.outcome", "invalid")
    retry_count = (state.get("retry_count") or 0) + 1
    errors = [str(exc)]
    return {
        "retry_count": retry_count,
        "validation_errors": errors,
    }
```

### 2.6 Drafting Graph Topology (`backend/core/graph.py`)

```python
def route_after_validation(state: ChatbotState) -> str:
    if state.get("current_draft") is not None:
        return "__end__"
    if (state.get("retry_count") or 0) < 2:
        return "draft_llm"
    return "error_handler"

def build_drafting_graph(runtime, checkpointer):
    # ... existing nodes ...
    from backend.nodes.error_handler import error_handler
    g.add_node("error_handler", _bind(error_handler, runtime))
    # existing edges ...
    g.add_conditional_edges(
        "schema_validate",
        route_after_validation,
        {"__end__": END, "draft_llm": "draft_llm", "error_handler": "error_handler"},
    )
    g.add_edge("error_handler", END)
    return g.compile(
        checkpointer=checkpointer,
        interrupt_after=["schema_validate"],
    )
```

### 2.7 error_handler Node (`backend/nodes/error_handler.py`)

```python
def error_handler(state, config, runtime):
    errors = state.get("validation_errors") or []
    reply = (
        "Unable to produce a valid BRD after 2 retries. "
        f"Validation errors: {'; '.join(errors)}. "
        "Please try again or adjust your requirements."
    )
    return {"reply_text": reply}
```

### 2.8 Assembler — Validation Errors Injection (`backend/context/assembler.py`)

Add `validation_errors` parameter to `assemble()`:

```python
def assemble(
    self,
    *,
    strategy: ContextStrategy,
    session_id: str,
    messages_in_state: list[BaseMessage],
    brd_memory: Optional[dict] = None,
    rolling_summary: Optional[dict] = None,
    current_draft: Optional[dict] = None,
    feedback: Optional[str] = None,
    artifact_refs: Optional[list[dict]] = None,
    validation_errors: Optional[list[str]] = None,
) -> AssembledContext:
```

Inject validation errors block into the prompt when `validation_errors` is provided:

```python
# In assemble(), after building other blocks:
error_block = ""
if validation_errors:
    error_block = (
        "PREVIOUS_VALIDATION_ERRORS (please fix these in your response):\n"
        + "\n".join(f"  - {e}" for e in validation_errors)
    )
```

Pass `error_block` into the system prompt stack before JSON instructions.

### 2.9 retrieve_context — Pass validation_errors (`backend/nodes/retrieve_context.py`)

In `retrieve_context`, read `validation_errors` from state and pass to assembler:

```python
validation_errors = state.get("validation_errors")

assembled = runtime.assembler.assemble(
    ...,
    validation_errors=validation_errors,
)
```

### 2.10 /chat Endpoint — HITL 1 Handling (`backend/api/chat.py`)

The `/chat` endpoint must catch `GraphInterrupt` from the gathering graph:

```python
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
    # ... mode decision ...
    graph = request.app.state.gathering_graph
    try:
        result = graph.invoke(..., config=_config(body.session_id))
    except GraphInterrupt as exc:
        # HITL 1 triggered
        interrupt_value = exc.args[0] if exc.args else {}
        return ChatResponse(
            reply="",
            mode="hitl_1",
            draft=None,
            turn_id=0,
            hitl={
                "type": "hitl_1",
                "summary": interrupt_value.get("summary"),
                "actions": interrupt_value.get("actions"),
            },
        )
    # ... normal handling ...
```

**Response model update:**
```python
class ChatResponse(BaseModel):
    reply: str
    mode: str
    draft: Optional[BRDResponse] = None
    turn_id: int
    hitl: Optional[dict] = None  # NEW: populated when interrupt triggers
```

### 2.11 New /resume Endpoint (`backend/api/chat.py`)

```python
class ResumeRequest(BaseModel):
    session_id: str
    action: str  # "proceed" or "add_more"

class ResumeResponse(BaseModel):
    reply: str
    mode: str
    draft: Optional[BRDResponse] = None
    turn_id: int

@router.post("/resume", response_model=ResumeResponse)
def resume(body: ResumeRequest, request: Request) -> ResumeResponse:
    graph = request.app.state.gathering_graph
    try:
        result = graph.invoke(
            Command(resume=body.action),
            config=_config(body.session_id),
        )
    except GraphInterrupt as exc:
        # Another HITL 1 (shouldn't happen in normal flow)
        ...

    reply = result.get("reply_text", "")
    draft = _current_draft(request, body.session_id)
    turn_id = _turn_id(request, body.session_id)
    mode = result.get("mode", "gathering")

    return ResumeResponse(reply=reply, mode=mode, draft=draft, turn_id=turn_id)
```

### 2.12 /generate-brd Endpoint — HITL 2 Handling (`backend/api/drafting.py`)

With `interrupt_after=["schema_validate"]`, the drafting graph pauses after successful validation. The endpoint catches this and returns the draft:

```python
from langgraph.errors import GraphInterrupt

@router.post("/generate-brd", response_model=DraftResponse)
def generate_brd(body: GenerateBRDRequest, request: Request) -> DraftResponse:
    graph = request.app.state.drafting_graph
    try:
        result = graph.invoke(...)
    except GraphInterrupt:
        # HITL 2: graph paused after schema_validate
        state = _read_graph_state(request, body.session_id)
        draft_dict = state.get("current_draft")
        if draft_dict:
            draft = BRDResponse.model_validate(draft_dict)
            return DraftResponse(draft=draft, mode="awaiting_approval")
        raise RuntimeError("schema_validate completed but no current_draft")

    # Normal completion (shouldn't happen with interrupt_after)
    draft_dict = result.get("current_draft")
    ...
```

### 2.13 Frontend — HITL 1 UI (`frontend/streamlit_app.py`)

Add HITL 1 banner when backend returns `mode == "hitl_1"`:

```python
# After chat response
if res.get("mode") == "hitl_1":
    st.session_state.hitl_1 = res["hitl"]
    st.rerun()

# Display HITL 1 controls
if st.session_state.get("hitl_1"):
    hitl = st.session_state["hitl_1"]
    st.info(f"📋 {hitl['summary']['title']}")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ Proceed to production"):
            res = api_resume("proceed", st.session_state.session_id)
            st.session_state.hitl_1 = None
            # Now call generate-brd
            res_gen = api_generate(st.session_state.session_id)
            draft = res_gen["draft"]
            st.session_state.current_draft = draft
            st.session_state.messages.append({
                "role": "brd_draft",
                "brd_id": draft["brd_id"],
                "content": draft,
            })
            st.rerun()
    with col2:
        if st.button("➕ Add more details"):
            res = api_resume("add_more", st.session_state.session_id)
            st.session_state.hitl_1 = None
            st.session_state.messages.append({
                "role": "assistant",
                "content": res["reply"],
            })
            st.rerun()
```

---

## 3. Verification Steps

1. **Import sweep:** `python -c "from backend.main import app; print('OK')"`
2. **Chat test:** Send 3–4 gathering messages. On the message that triggers `[READY_FOR_PRODUCTION]`, verify `/chat` returns `mode: "hitl_1"` with summary.
3. **HITL 1 proceed:** Call `/resume` with `"proceed"`. Verify mode transitions to `"drafting"`.
4. **HITL 1 add more:** Call `/resume` with `"add_more"`. Verify chat continues in gathering mode.
5. **Drafting with retry:** Temporarily break schema validation (e.g., by modifying the draft_llm prompt to omit a required field). Verify retry loop executes up to 2 times, then routes to `error_handler`.
6. **HITL 2:** After successful drafting, verify `/generate-brd` returns draft with `mode: "awaiting_approval"`.
7. **Full loop:** gather → HITL 1 (proceed) → draft → HITL 2 → request changes → gather → HITL 1 (proceed) → re-draft.

---

## 4. Exit Criteria

- [ ] Gathering graph has conditional edge after `invoke_llm` → `hitl_gate` | `extract_memory`.
- [ ] `hitl_gate` calls `interrupt()` and returns `mode: "drafting"` on proceed.
- [ ] `/chat` catches `GraphInterrupt` and returns HITL 1 payload.
- [ ] `/resume` endpoint accepts `Command(resume=...)` to continue from interrupt.
- [ ] Drafting graph has conditional edge after `schema_validate` → END | `draft_llm` (retry) | `error_handler`.
- [ ] `schema_validate` no longer raises on failure; sets `retry_count` + `validation_errors`.
- [ ] `error_handler` returns graceful error message after max retries.
- [ ] Drafting graph compiled with `interrupt_after=["schema_validate"]`.
- [ ] Frontend shows HITL 1 summary + Proceed/Add More buttons.
- [ ] Full loop works end-to-end: gather → HITL 1 → draft → retry on failure → HITL 2 → approve/reject → re-gather.

---

## 5. Risks & Mitigations

| Risk | Mitigation |
|:---|:---|
| LLM doesn't reliably emit `[READY_FOR_PRODUCTION]` | Add turn-count fallback (auto-ready after N turns); tune system prompt |
| `interrupt()` API differs from documentation | Verify during implementation with small test script |
| `Command(resume=...)` requires langgraph>=0.3 | Pin version in requirements.txt if needed |
| Validation retry loop changes drafting graph timing | Ensure `retry_count` is reset to 0 at start of each `/generate-brd` |
| Streamlit state conflicts with HITL 1/2 | Use distinct st.session_state keys (`hitl_1`, `current_draft`) |

---

## 6. Discrepancy Notes (to be filled during execution)

> Any deviations discovered during implementation will be logged here and copied to `discrepancy_notes.md`.
