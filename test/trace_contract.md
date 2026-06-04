# BRD Agent — Trace Contract

**Owner:** BRD agent (PoC) · **Status:** v0.1 (Phase 6 deliverable) · **Reference standards:** [ws8_chassis/docs/trace_contract.md](../../ws8_chassis/docs/trace_contract.md), [OpenInference SemConv](https://github.com/Arize-ai/openinference/tree/main/spec), [OpenTelemetry GenAI SemConv](https://opentelemetry.io/docs/specs/semconv/gen-ai/)

This document is the contract for what every span emitted by the BRD agent must look like. Phoenix renders against these conventions; new sources / nodes added in future phases conform to them.

---

## 1. Resource attributes (per process)

Attached once at startup by `backend/telemetry.init_telemetry()`:

| Attribute | Value | Source |
| --- | --- | --- |
| `service.name` | `brd-agent` (or `$OTEL_SERVICE_NAME`) | constant |
| `service.namespace` | `brd-poc` | constant — groups brd_agent + ws8_chassis runs in Phoenix |
| `service.version` | `0.1.0` | constant |
| `deployment.environment` | `local` (or `$DEPLOYMENT_ENVIRONMENT`) | env var or constant |

---

## 2. Universal span attributes (every span)

Attached automatically by `telemetry.chat_span(...)`. Sources also inherit these via `telemetry.source_span(...)`, which delegates to `chat_span` internally.

| Attribute | Type | Set by | Notes |
| --- | --- | --- | --- |
| `openinference.span.kind` | string | helper | One of `AGENT / CHAIN / TOOL / RETRIEVER / LLM`. Drives Phoenix's kind badge. |
| `session.id` | string (uuid hex) | helper | LangGraph thread id; rotated on `/reset`. |
| `user.id` | string | helper | Synthetic `synthetic-poc-user` until WS#3 attaches a real principal. |
| `agent.id` | string | helper | `brd-agent`. |
| `agent.version` | string | helper | `0.1.0`. |
| `principal.tenant_id` | string | helper | `northern-trust` (synthetic placeholder). |
| `principal.user_oid_hash` | string (hex) | helper | `sha256(synthetic-poc-user)`. |
| `turn.id` | string | helper (if passed) | Monotonic per-turn integer, or `draft` / `approve` / `reset`. |
| `chat.mode` | string | helper (if passed) | `gathering / drafting / request_changes / approve / reset / tool_call`. |
| `tag.tags` | string list | helper (if passed) | Root-level filter chips (`gathering` / `drafting` / `request_changes` / `approve` / `reset` / `tool`). |

---

## 3. Span tree per user action

### `brd_agent.turn` (root, AGENT) — `POST /chat` in gathering mode

```
brd_agent.turn                                     [AGENT, SERVER, tags=["gathering"]]
├── summarize                                      [CHAIN]      runs only when len(state.messages) > 12
│   └── ChatCompletion                             [LLM, auto]  rolling-summary LLM call
├── retrieve_context                               [CHAIN]      assembled prompt
│   ├── context.retrieve.docs                      [RETRIEVER]  Chroma — see §4
│   └── context.retrieve.kg                        [RETRIEVER]  Neo4j — see §4
├── invoke_llm                                     [CHAIN]      gathering LLM call
│   └── ChatCompletion                             [LLM, auto]
└── extract_memory                                 [CHAIN]      LangMem manager.invoke
    └── ChatCompletion                             [LLM, auto]  BRDMemory extraction
```

### `brd_agent.draft` (root, AGENT) — `POST /generate-brd`

```
brd_agent.draft                                    [AGENT, SERVER, tags=["drafting"]]
├── retrieve_context                               [CHAIN, strategy=BRD_GENERATION]
│   ├── context.retrieve.docs                      [RETRIEVER, k=4, focus=template+priors]
│   └── context.retrieve.kg                        [RETRIEVER, k=4]
├── draft_llm                                      [CHAIN, response_format=json_object]
│   └── ChatCompletion                             [LLM, auto]
└── schema_validate                                [TOOL]       BRDResponse.model_validate
```

### `brd_agent.request_changes` (root, AGENT) — `POST /request-changes`

Same shape as `brd_agent.turn`, with `chat.mode="request_changes"`, `tags=["request_changes"]`, `draft.id=<brd_id>` on the root, and strategy `REFINEMENT` driving the retrieve_context behaviour (draft + memory + summary all injected via the assembler's draft_block).

### `brd_agent.approve` (root, CHAIN) — `POST /approve`

No retrieval, no LLM. Root carries `brd.id`, `approval.outcome="approved"`, `agent.outcome="approved"`.

### `brd_agent.reset` (root, CHAIN) — `POST /reset`

Root only. `agent.outcome="reset"`. The new `session.id` is set on the span.

### `tool.fetch_brd_template` (root, TOOL) — `POST /tools/fetch-brd-template`

```
tool.fetch_brd_template                            [TOOL, SERVER, tags=["tool"]]
  attrs: artifact.{id,type,name,size_bytes}, input.value, output.value
```

The next `brd_agent.turn` then surfaces the artifact summary into the assembled prompt — see `prompt.tokens.artifacts` on that turn's `retrieve_context`.

---

## 4. Source spans — `context.retrieve.*`

All retrieval sources (Vector DB, KG, future artifact lookups) open spans via `telemetry.source_span(...)` so the attribute schema is uniform.

| Attribute | Type | Required | Notes |
| --- | --- | --- | --- |
| `openinference.span.kind` | string | yes | Always `RETRIEVER`. |
| `source.kind` | string | yes | Backend identifier (`vector_store` for Chroma, `kg` for Neo4j). |
| `source.query` | string | yes (when query was used) | Truncated to 200 chars. |
| `source.k` | int | yes | Requested hit count. |
| `source.focus` | string | optional | Advisory hint for the source (e.g. `glossary` / `template+priors`). |
| `source.hits_count` | int | yes | Actual count returned (≤ k). |
| `source.hit_ids` | string (csv) | when hits>0 | Backend-specific IDs joined by `,`. |
| `source.distance_min` | float | when applicable | For embedding-distance sources. KG sources omit. |
| `source.duration_ms` | float | yes | Set automatically by `source_span` on exit. |
| `kg.entity_labels` | string (csv) | KG only | Entity labels matched (`Stakeholder,BusinessUnit,…`). |
| `kg.neighbours_total` | int | KG only | Sum of 1-hop neighbour counts across hits. |

---

## 5. `retrieve_context` token accounting

Per-section token contribution surfaces as `prompt.tokens.*`:

| Attribute | Source |
| --- | --- |
| `prompt.tokens.system_prompt` | the gathering or drafting system prompt |
| `prompt.tokens.memory` | rendered BRDMemory block (LangMem) |
| `prompt.tokens.summary` | rendered ConversationSummary block (summarize node) |
| `prompt.tokens.draft_block` | injected draft + feedback (REVIEW_FEEDBACK / REFINEMENT only) |
| `prompt.tokens.docs` | Chroma RELEVANT_REFERENCES block |
| `prompt.tokens.kg` | Neo4j KNOWLEDGE_GRAPH block |
| `prompt.tokens.artifacts` | TOOL_ARTIFACTS block from `state.last_retrievals.artifact_refs` |
| `prompt.tokens.conversation` | sliced message turns (per strategy policy) |
| `prompt.tokens.total` | sum of the above |
| `retrieve.strategy` | `ContextStrategy` value chosen by `strategy.infer_strategy()` |
| `retrieve.has_memory / has_summary / has_draft` | boolean shortcuts |
| `retrieve.artifact_refs_count` | number of ArtifactRefs surfaced this turn |
| `context.dropped_sections` | csv of sections dropped under budget (e.g. `stm_oldest_turns=5`) |

---

## 6. Outcome enum (root spans, `agent.outcome`)

Set by `chat_span(..., track_outcome=True)` on the success path (caller sets via `set_outcome(span, ...)`) and on exception path (helper sets `"error"`).

| Value | Where it's set |
| --- | --- |
| `success` | `brd_agent.{turn,draft,request_changes}` + `tool.fetch_brd_template` on clean completion |
| `error` | Any root span that raises |
| `schema_error` | `brd_agent.draft` when `BRDResponse.model_validate` raises |
| `approved` | `brd_agent.approve` |
| `reset` | `brd_agent.reset` |

---

## 7. Anti-patterns

| Don't | Why |
| --- | --- |
| Put full prompt/completion in attributes | Use OpenInference auto-instrumented ChatCompletion events instead. They carry the full content for drill-down without polluting filter indexes. |
| Skip `source_span` for a new retrieval backend | Phoenix queries (`source.kind = …`, `source.duration_ms > 100`) rely on uniform attribute names. Reinventing breaks Phoenix's groupings. |
| Add per-source LLM call spans manually | OpenInference's openai instrumentor catches them automatically because every source's LLM (LangMem manager, summarize) goes through the `openai` SDK. |
| Mark `chat.draft` ERROR on schema-validate failure | Set `agent.outcome="schema_error"` instead. ERROR status is reserved for system failures the caller should retry. |
| Skip closing a span on the error path | All spans must exit. `chat_span` / `source_span` use context managers exactly to enforce this. |

---

## 8. Verification cheatsheet (Phoenix UI)

- Filter: `service.name = "brd-agent"`
- Group by `session.id` to see a whole conversation across turns.
- Filter `tag.tags CONTAINS "drafting"` to find draft attempts.
- Filter `agent.outcome != "success"` to find failed turns.
- Filter `openinference.span.kind = "RETRIEVER"` to see all source calls; group by `source.kind` to compare backends.
- Drill into `ChatCompletion` LLM spans for full prompt + completion in span events.
