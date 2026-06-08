# Phase 4 Plan — Draft Versioning & Observability

> **Goal:** Add append-only `draft_history` with full provenance. Add `/drafts` API endpoints.
> **Depends on:** Phase 2 (graph topology, HITL, retry loop)
> **Est. effort:** 1 day

---

## 1. What We Are Changing

| # | Change | File(s) |
|:---|:---|:---|
| 1 | On successful schema validation, append a versioned entry to `state.draft_history` with provenance (prompt hash, strategy, model, token accounting) | `backend/nodes/schema_validate.py` |
| 2 | On `/approve`, update the latest `draft_history` entry with `reviewed_by` + `reviewed_at` + `status="approved"` | `backend/api/review.py` |
| 3 | On `/request-changes`, update the latest `draft_history` entry with `reviewed_by` + `reviewed_at` + `status="rejected"` | `backend/api/review.py` |
| 4 | Add `GET /drafts` and `GET /drafts/{version}` endpoints | `backend/api/drafting.py` |
| 5 | Add `DraftHistoryEntry` Pydantic model for API contract | `backend/core/schema.py` |
| 6 | Verify model version pinning on LLM spans | `backend/nodes/invoke_llm.py`, `backend/nodes/draft.py`, `backend/nodes/summarize.py` |
| 7 | Unit + integration tests | `tests/unit/test_draft_versioning.py` |

---

## 2. Draft History Entry Format

Stored as plain `dict` in `ChatbotState.draft_history` (list reducer is standard list overwrite, so we read existing, append, and return the full list).

```python
{
    "version": int,                          # monotonic 1-based
    "draft": dict,                           # cleaned BRDResponse model_dump
    "produced_at": "2026-06-08T12:00:00+00:00",
    "prompt_id": str,
    "prompt_version": str,
    "prompt_hash": "sha256:<hex>",         # assembled.prompt_template_hash prefixed
    "context_strategy": str,                 # e.g. "BRD_GENERATION"
    "model": str,                            # runtime.llm.model
    "provider": str,                         # runtime.llm.provider
    "token_accounting": dict,                # assembled.token_accounting dump
    "status": "draft",                       # → "approved" | "rejected"
    "reviewed_by": None,                     # → "user" (or future auth)
    "reviewed_at": None,                     # → ISO timestamp
}
```

---

## 3. Implementation Details

### 3.1 `schema_validate.py` — append on success

On the **success path only** (after `BRDResponse.model_validate` and after Presidio scan):

1. Read `assembled = (state.get("last_retrievals") or {}).get("assembled") or {}`
2. Read `history = state.get("draft_history") or []`
3. Build entry dict with all provenance fields from `assembled` + `runtime.llm`
4. Append entry → `history.append(entry)`
5. Return updated state:
   ```python
   {
       "current_draft": cleaned_draft,
       "draft_status": DraftStatus.DRAFT,
       "retry_count": 0,
       "validation_errors": [],
       "draft_history": history,
   }
   ```
6. Add span attributes:
   - `draft.version`
   - `draft.history_count`
   - `draft.produced_at`

**Nota bene:** Failure path (retry) does **not** append to `draft_history` — only validated drafts become versions.

### 3.2 `review.py` — mutate latest entry on approval / rejection

**`/approve`**:
- After `graph.update_state(config, {"draft_status": DraftStatus.APPROVED})`, also read `draft_history`, copy it, mutate the last entry:
  ```python
  history = list(state.get("draft_history") or [])
  if history:
      history[-1]["status"] = "approved"
      history[-1]["reviewed_by"] = "user"
      history[-1]["reviewed_at"] = datetime.now(timezone.utc).isoformat()
      graph.update_state(config, {"draft_history": history})
  ```
- Add span attributes: `approval.reviewed_at`, `approval.reviewed_by`

**`/request-changes`**:
- Do the same mutation with `status = "rejected"` before or after the existing `graph.update_state` call.

### 3.3 `drafting.py` — new endpoints

Add two GET endpoints to the existing router:

```python
@router.get("/drafts")
def list_drafts(session_id: str, request: Request) -> DraftsListResponse:
    state = _read_graph_state(request, session_id)
    history = state.get("draft_history") or []
    versions = [e["version"] for e in history]
    latest_status = history[-1]["status"] if history else None
    return DraftsListResponse(session_id=session_id, versions=versions, count=len(history), latest_status=latest_status)

@router.get("/drafts/{version}")
def get_draft(version: int, session_id: str, request: Request) -> DraftDetailResponse:
    state = _read_graph_state(request, session_id)
    history = state.get("draft_history") or []
    entry = next((e for e in history if e["version"] == version), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Draft version {version} not found.")
    draft = BRDResponse.model_validate(entry["draft"])
    return DraftDetailResponse(session_id=session_id, entry=entry, draft=draft)
```

Response models (in `backend/core/schema.py` or inline):

```python
class DraftsListResponse(BaseModel):
    session_id: str
    versions: list[int]
    count: int
    latest_status: Optional[str] = None

class DraftDetailResponse(BaseModel):
    session_id: str
    entry: dict  # the full DraftHistoryEntry dict
    draft: BRDResponse
```

**Alternative:** Keep response models lightweight — `DraftsListResponse` only returns version numbers and statuses; `DraftDetailResponse` returns the full entry + parsed BRD. This avoids bloating the list endpoint with full BRD payloads.

### 3.4 Model version pinning (verify)

The following nodes already set `llm.model` + `llm.provider` on their spans:
- `invoke_llm.py` ✅
- `draft.py` ✅
- `summarize.py` ✅

No action required unless a gap is found during review.

---

## 4. Testing Strategy

### 4.1 Unit tests (`tests/unit/test_draft_versioning.py`)

1. **`test_schema_validate_appends_history`** — mock state with `last_retrievals.assembled` + empty `draft_history`; assert returned state has `draft_history` with one entry, version=1, correct provenance fields.
2. **`test_schema_validate_preserves_existing_history`** — pre-populate `draft_history` with one entry; assert new entry gets version=2.
3. **`test_schema_validate_failure_does_not_append`** — invalid JSON; assert `draft_history` is absent from return value (or unchanged).
4. **`test_approve_updates_latest_entry`** — mock graph state with `draft_history=[{...}]`; assert mutation sets `status=approved`, `reviewed_by`, `reviewed_at`.
5. **`test_request_changes_updates_latest_entry`** — same as above but `status=rejected`.
6. **`test_list_drafts_empty`** — empty history → `versions=[]`, `count=0`.
7. **`test_get_draft_not_found`** — version 99 → 404.

### 4.2 Integration / manual verification

1. Run smoke chat flow through HITL 1 → generate BRD → HITL 2 (approve).
2. Query `GET /drafts?session_id=xxx` → expect `versions=[1]`, `latest_status="approved"`.
3. Query `GET /drafts/1?session_id=xxx` → expect full entry with provenance.
4. Run a second production cycle (request changes, re-gather, generate again) → expect `versions=[1,2]`.
5. Verify Phoenix spans show `llm.model` on `invoke_llm`, `draft_llm`, and `summarize`.

---

## 5. Exit Criteria

- [ ] After a successful BRD production, `state.draft_history` contains 1 entry with `version=1`, full provenance, and `status="draft"`.
- [ ] After `/approve`, the latest entry shows `status="approved"`, `reviewed_by="user"`, `reviewed_at` set.
- [ ] After `/request-changes`, the latest entry shows `status="rejected"`, `reviewed_by`, `reviewed_at` set.
- [ ] `GET /drafts` returns version list + count + latest status.
- [ ] `GET /drafts/{version}` returns the full entry + parsed `BRDResponse`.
- [ ] All 7+ unit tests pass.
- [ ] App imports correctly (`python -c "import backend.main"`).
