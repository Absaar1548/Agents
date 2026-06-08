# Phase 3 — Presidio Guardrails

> **Goal:** Add PII scanning wrappers on every LLM call per Chassis §3.5 and SOW §7.
> **Depends on:** Phase 1 (session management is done; guardrails are independent of graph topology)
> **Est. effort:** 1 day

---

## 1. Overview

The SOW explicitly states: **"PII/PCI data must not be uploaded to any LLM"**. Phase 3 implements this by wrapping Presidio Analyzer + Anonymizer around the four LLM-adjacent surfaces:

| # | Surface | Direction | What gets scanned |
|:---|:---|:---|:---|
| 1 | Before `invoke_llm` | Input → LLM | Assembled prompt messages (user content + system context) |
| 2 | After `invoke_llm` | LLM → User | Assistant reply text |
| 3 | Before `draft_llm` | Input → LLM | Assembled drafting prompt messages |
| 4 | After `schema_validate` | LLM → User | Generated BRD JSON (string fields recursively) |

**Entity scope** (locked decision from master plan):

| Entity | Action |
|:---|:---|
| `CREDIT_CARD`, `US_SSN`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_BANK_NUMBER`, `IBAN_CODE` | **Mask** (replace with `<MASKED_ENTITY_TYPE>`) |
| `PERSON`, `IP_ADDRESS` | **Log only** (don't mask — BRDs legitimately contain stakeholder names) |

---

## 2. Files

### Create
- (none — `backend/guardrails/presidio.py` already exists as a stub)

### Modify
| File | Change |
|:---|:---|
| `backend/guardrails/presidio.py` | Replace stub with full implementation: `AnalyzerEngine`, `AnonymizerEngine`, `scan_and_protect()`, `_mask_messages()` |
| `backend/guardrails/__init__.py` | Export `scan_and_protect` |
| `backend/nodes/invoke_llm.py` | Scan assembled messages before LLM call; scan reply after LLM call |
| `backend/nodes/draft.py` | Scan assembled messages before LLM call |
| `backend/nodes/schema_validate.py` | Scan generated BRD on success before returning |
| `backend/telemetry.py` | Add `guardrail_span()` context manager |
| `requirements.txt` | Add `presidio-analyzer>=2.2`, `presidio-anonymizer>=2.2` |
| `scripts/setup.ps1` | Pre-download spaCy model (`en_core_web_sm`) |

---

## 3. Detailed Implementation

### 3.1 `backend/guardrails/presidio.py` — Core Engine

Replace the stub with a production implementation:

```python
"""Presidio PII guardrails.

Uses Microsoft Presidio Analyzer + Anonymizer to detect and mask sensitive
entities before text reaches an LLM or before LLM output reaches the user.

Entity policy (locked):
  MASK:   CREDIT_CARD, US_SSN, EMAIL_ADDRESS, PHONE_NUMBER,
          US_BANK_NUMBER, IBAN_CODE
  LOG-ONLY (don't mask): PERSON, IP_ADDRESS

SpaCy model dependency:
  Presidio's built-in SpacyRecognizer requires a spaCy NER model for PERSON
detection. If the model is missing, the engine gracefully falls back to
regex-based recognizers only; PERSON detection will not work but all other
entities (SSN, CC, email, phone, etc.) are regex-based and still function.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from backend.telemetry import KIND_TOOL, chat_span

logger = logging.getLogger(__name__)

# Entities to actively MASK in prompts and outputs
MASK_ENTITIES = [
    "CREDIT_CARD",
    "US_SSN",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_BANK_NUMBER",
    "IBAN_CODE",
]

# Entities to LOG but NOT mask (legitimate in BRD context)
LOG_ONLY_ENTITIES = ["PERSON", "IP_ADDRESS"]

ALL_ENTITIES = MASK_ENTITIES + LOG_ONLY_ENTITIES

# Singleton engines (lazy-initialized)
_analyzer: Optional[AnalyzerEngine] = None
_anonymizer: Optional[AnonymizerEngine] = None


def _init_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    """Lazy-init Presidio engines with graceful spaCy fallback."""
    global _analyzer, _anonymizer
    if _analyzer is not None and _anonymizer is not None:
        return _analyzer, _anonymizer

    try:
        _analyzer = AnalyzerEngine()
    except Exception as exc:
        logger.warning(
            "Presidio AnalyzerEngine failed to load (spacy model missing?): %s. "
            "Falling back to regex-only recognizers. PERSON detection disabled.",
            exc,
        )
        # Fallback: create analyzer with only built-in non-spacy recognizers
        from presidio_analyzer.recognizer_registry import RecognizerRegistry
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
            PhoneRecognizer,
            UsSsnRecognizer,
            UsBankRecognizer,
            IbanRecognizer,
            IpRecognizer,
        )
        registry = RecognizerRegistry()
        for RecClass in (
            CreditCardRecognizer,
            EmailRecognizer,
            PhoneRecognizer,
            UsSsnRecognizer,
            UsBankRecognizer,
            IbanRecognizer,
            IpRecognizer,
        ):
            registry.add_recognizer(RecClass())
        _analyzer = AnalyzerEngine(registry=registry)

    _anonymizer = AnonymizerEngine()
    return _analyzer, _anonymizer


def scan_and_protect(
    text: str,
    *,
    session_id: Optional[str] = None,
    scan_name: str = "guardrails.presidio",
) -> tuple[str, list[dict], list[dict]]:
    """Scan text for PII and mask sensitive entities.

    Args:
        text: The text to scan.
        session_id: Optional session ID for telemetry.
        scan_name: Span name segment (e.g. "guardrails.invoke_llm.input").

    Returns:
        Tuple of (cleaned_text, masked_findings, logged_findings).
        masked_findings: list of dicts with keys entity_type, start, end, score.
        logged_findings: list of dicts with keys entity_type, start, end, score.
    """
    analyzer, anonymizer = _init_engines()

    with chat_span(
        scan_name,
        session_id=session_id or "",
        span_kind=KIND_TOOL,
    ) as span:
        span.set_attribute("guardrail.type", "presidio")
        span.set_attribute("guardrail.input_length", len(text))

        if not text:
            span.set_attribute("guardrail.entity_count", 0)
            return text, [], []

        results = analyzer.analyze(text=text, entities=ALL_ENTITIES, language="en")

        # Split into mask vs log-only
        mask_results: list[RecognizerResult] = []
        log_results: list[RecognizerResult] = []
        for r in results:
            if r.entity_type in MASK_ENTITIES:
                mask_results.append(r)
            elif r.entity_type in LOG_ONLY_ENTITIES:
                log_results.append(r)

        # Build findings metadata (never include the original text)
        masked_findings = [
            {
                "entity_type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "score": round(r.score, 3),
            }
            for r in mask_results
        ]
        logged_findings = [
            {
                "entity_type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "score": round(r.score, 3),
            }
            for r in log_results
        ]

        # Anonymize masked entities
        operators = {
            entity: OperatorConfig("replace", {"new_value": f"<{entity}>"})
            for entity in MASK_ENTITIES
        }
        if mask_results:
            cleaned = anonymizer.anonymize(
                text=text, analyzer_results=mask_results, operators=operators
            ).text
        else:
            cleaned = text

        # Telemetry
        span.set_attribute("guardrail.entity_count", len(results))
        span.set_attribute("guardrail.masked_count", len(mask_results))
        span.set_attribute("guardrail.logged_count", len(log_results))
        if masked_findings:
            span.set_attribute(
                "guardrail.masked_entities",
                json.dumps([f["entity_type"] for f in masked_findings]),
            )
        if logged_findings:
            span.set_attribute(
                "guardrail.logged_entities",
                json.dumps([f["entity_type"] for f in logged_findings]),
            )

        return cleaned, masked_findings, logged_findings


def _mask_messages(
    messages: list[dict],
    *,
    session_id: Optional[str] = None,
    scan_name: str = "guardrails.messages",
) -> tuple[list[dict], list[dict], list[dict]]:
    """Scan and mask the `content` field of each OpenAI-format message dict.

    Returns:
        (cleaned_messages, all_masked_findings, all_logged_findings)
    """
    cleaned_messages: list[dict] = []
    all_masked: list[dict] = []
    all_logged: list[dict] = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if not content:
            cleaned_messages.append(msg)
            continue
        cleaned, masked, logged = scan_and_protect(
            content,
            session_id=session_id,
            scan_name=f"{scan_name}.msg_{i}",
        )
        cleaned_msg = dict(msg)
        cleaned_msg["content"] = cleaned
        cleaned_messages.append(cleaned_msg)
        all_masked.extend(masked)
        all_logged.extend(logged)
    return cleaned_messages, all_masked, all_logged


def scan_dict_strings(
    data: dict,
    *,
    session_id: Optional[str] = None,
    scan_name: str = "guardrails.dict",
) -> tuple[dict, list[dict], list[dict]]:
    """Recursively scan all string values in a dict and mask PII.

    Returns:
        (cleaned_dict, all_masked_findings, all_logged_findings)
    """
    all_masked: list[dict] = []
    all_logged: list[dict] = []

    def _scan(obj: Any) -> Any:
        if isinstance(obj, str):
            cleaned, masked, logged = scan_and_protect(
                obj, session_id=session_id, scan_name=scan_name
            )
            all_masked.extend(masked)
            all_logged.extend(logged)
            return cleaned
        if isinstance(obj, dict):
            return {k: _scan(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_scan(v) for v in obj]
        return obj

    cleaned = _scan(data)
    return cleaned, all_masked, all_logged
```

**Design decisions:**
- **Lazy singleton engines:** `_init_engines()` is called on first `scan_and_protect()` invocation. This avoids heavy import-time work and lets the app start even if Presidio is misconfigured.
- **Graceful spaCy fallback:** If `AnalyzerEngine()` fails (typically because `en_core_web_sm` is missing), we construct a `RecognizerRegistry` with only regex-based recognizers. All `MASK_ENTITIES` are regex-based and will still work. `PERSON` requires spaCy NER and will be silently skipped in fallback mode.
- **OperatorConfig:** Uses `replace` with `<ENTITY_TYPE>` placeholder (e.g. `<CREDIT_CARD>`). This is deterministic and clearly signals to both the LLM and the user that data was masked.
- **No original text in findings:** The `masked_findings` / `logged_findings` dicts only contain position, type, and confidence score — never the raw substring.

### 3.2 `backend/nodes/invoke_llm.py` — Points 1 & 2

Insert guardrail calls around the LLM completion:

```python
from backend.guardrails.presidio import _mask_messages, scan_and_protect

# ... inside invoke_llm ...

messages = assembled.get("messages") or []

# Point 1: scan assembled messages before sending to LLM
messages, input_masked, input_logged = _mask_messages(
    messages,
    session_id=session_id,
    scan_name="guardrails.invoke_llm.input",
)
# Tag span with input findings
span.set_attribute("guardrail.input.masked_count", len(input_masked))
span.set_attribute("guardrail.input.logged_count", len(input_logged))

reply = runtime.llm.complete(messages=messages, temperature=0.4, max_tokens=800)

# Point 2: scan reply before returning to user
clean_reply, output_masked, output_logged = scan_and_protect(
    reply,
    session_id=session_id,
    scan_name="guardrails.invoke_llm.output",
)
span.set_attribute("guardrail.output.masked_count", len(output_masked))
span.set_attribute("guardrail.output.logged_count", len(output_logged))

# Continue with READY_MARKER detection on clean_reply instead of reply
ready = READY_MARKER in clean_reply
```

**Note:** We detect `READY_MARKER` on the *cleaned* reply (after masking). This is safe because the marker is generated by the LLM and won't contain PII.

### 3.3 `backend/nodes/draft.py` — Point 3

Insert guardrail call before the LLM completion:

```python
from backend.guardrails.presidio import _mask_messages

# ... inside draft_llm ...

messages = assembled.get("messages") or []

# Point 3: scan assembled drafting messages before sending to LLM
messages, input_masked, input_logged = _mask_messages(
    messages,
    session_id=session_id,
    scan_name="guardrails.draft_llm.input",
)
span.set_attribute("guardrail.input.masked_count", len(input_masked))
span.set_attribute("guardrail.input.logged_count", len(input_logged))

raw = runtime.llm.complete(
    messages=messages,
    response_format={"type": "json_object"},
    max_tokens=4000,
    temperature=0.2,
)
```

### 3.4 `backend/nodes/schema_validate.py` — Point 4

On success, scan the generated BRD before returning:

```python
from backend.guardrails.presidio import scan_dict_strings

# ... inside schema_validate, in the success branch ...

brd = BRDResponse.model_validate(data)

# Point 4: scan generated BRD before it reaches user / reviewer
cleaned_draft, masked, logged = scan_dict_strings(
    brd.model_dump(mode="json"),
    session_id=session_id,
    scan_name="guardrails.schema_validate.output",
)
span.set_attribute("guardrail.output.masked_count", len(masked))
span.set_attribute("guardrail.output.logged_count", len(logged))

# Use cleaned_draft instead of brd.model_dump(mode="json")
return {
    "current_draft": cleaned_draft,
    "draft_status": DraftStatus.DRAFT,
    "retry_count": 0,
    "validation_errors": [],
}
```

**Why `scan_dict_strings` instead of `scan_and_protect`?** The BRD is a structured JSON dict. If we serialized it to a single string, masked, and deserialized, the JSON structure could be corrupted (e.g. a masked email inside a JSON key). Recursive field scanning preserves structure.

### 3.5 `backend/telemetry.py` — Guardrail Span Helper

Add a lightweight wrapper:

```python
@contextmanager
def guardrail_span(
    name: str,
    *,
    session_id: str,
    guardrail_type: str = "presidio",
    turn_id: str | int | None = None,
) -> Iterator[Span]:
    """Open a span tagged with guardrail-specific attributes."""
    with chat_span(
        name,
        session_id=session_id,
        span_kind=KIND_TOOL,
        otel_kind=SpanKind.INTERNAL,
        turn_id=turn_id,
    ) as span:
        span.set_attribute("guardrail.type", guardrail_type)
        yield span
```

Actually, looking at the implementation above, `scan_and_protect` already opens a `chat_span(..., span_kind=KIND_TOOL)` internally. We don't *need* a separate helper in telemetry.py; the guardrail module can just use `chat_span` directly. However, adding `guardrail_span` as a semantic alias is nice for future-proofing (e.g. if we add AWS Comprehend later). I'll keep it as a **stretch / optional** — the plan works fine without it.

### 3.6 `requirements.txt`

Add after the Observability block:

```
# Guardrails (Phase 3)
presidio-analyzer>=2.2
presidio-anonymizer>=2.2
spacy>=3.7               # optional; required for PERSON detection
```

### 3.7 `scripts/setup.ps1`

Add a step after `pip install`:

```powershell
Write-Host "Downloading spaCy model for Presidio PERSON detection (optional but recommended)..."
python -m spacy download en_core_web_sm
```

**Rationale:** On first run, Presidio's default `AnalyzerEngine` tries to load `en_core_web_sm`. Pre-downloading avoids runtime latency and the fallback path.

---

## 4. Testing Strategy

### 4.1 Unit Test (no LLM required)
Create `tests/unit/test_guardrails.py`:

```python
import pytest
from backend.guardrails.presidio import scan_and_protect, _mask_messages, scan_dict_strings

CC_TEST = "My card is 4111 1111 1111 1111 and email is alice@example.com"
SSN_TEST = "SSN: 078-05-1120"
PHONE_TEST = "Call me at +1-650-555-1212"
PERSON_TEST = "Contact John Smith for details"

class TestScanAndProtect:
    def test_masks_credit_card(self):
        cleaned, masked, logged = scan_and_protect(CC_TEST)
        assert "4111" not in cleaned
        assert "<CREDIT_CARD>" in cleaned
        assert "alice@example.com" not in cleaned
        assert "<EMAIL_ADDRESS>" in cleaned
        assert any(f["entity_type"] == "CREDIT_CARD" for f in masked)
        assert any(f["entity_type"] == "EMAIL_ADDRESS" for f in masked)

    def test_masks_ssn(self):
        cleaned, masked, logged = scan_and_protect(SSN_TEST)
        assert "078-05-1120" not in cleaned
        assert "<US_SSN>" in cleaned

    def test_masks_phone(self):
        cleaned, masked, logged = scan_and_protect(PHONE_TEST)
        assert "650-555-1212" not in cleaned
        assert "<PHONE_NUMBER>" in cleaned

    def test_logs_person_without_masking(self):
        cleaned, masked, logged = scan_and_protect(PERSON_TEST)
        # "John Smith" should still be present (log-only)
        assert "John Smith" in cleaned
        assert any(f["entity_type"] == "PERSON" for f in logged)
        assert not any(f["entity_type"] == "PERSON" for f in masked)

    def test_empty_text(self):
        cleaned, masked, logged = scan_and_protect("")
        assert cleaned == ""
        assert masked == []
        assert logged == []

class TestMaskMessages:
    def test_masks_in_message_content(self):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My SSN is 078-05-1120"},
        ]
        cleaned, masked, logged = _mask_messages(msgs)
        assert cleaned[0]["content"] == "You are a helpful assistant."
        assert "<US_SSN>" in cleaned[1]["content"]
        assert any(f["entity_type"] == "US_SSN" for f in masked)

class TestScanDictStrings:
    def test_scans_nested_strings(self):
        data = {
            "title": "Project Alpha",
            "contact": "Email alice@example.com",
            "nested": {"phone": "+1-650-555-1212"},
            "items": ["SSN 078-05-1120", "safe text"],
        }
        cleaned, masked, logged = scan_dict_strings(data)
        assert "alice@example.com" not in cleaned["contact"]
        assert "+1-650-555-1212" not in cleaned["nested"]["phone"]
        assert "078-05-1120" not in cleaned["items"][0]
        assert cleaned["items"][1] == "safe text"
```

### 4.2 Smoke Test (requires backend running)

Use the existing smoke test framework. Add a new script `scripts/smoke_guardrails.py`:

```python
"""Smoke test: verify PII is masked before reaching LLM and before reaching user."""
import requests

BASE = "http://localhost:8000"

# Start a session with PII-laden input
r = requests.post(f"{BASE}/chat", json={
    "session_id": "smoke-guardrails-01",
    "message": "My credit card is 4111 1111 1111 1111 and SSN is 078-05-1120."
})
r.raise_for_status()
data = r.json()

# The reply should NOT contain the raw PII (the LLM might echo it back,
# but the guardrail on output should mask it).
reply = data.get("reply", "")
assert "4111" not in reply, f"Credit card leaked in reply: {reply[:200]}"
assert "078-05" not in reply, f"SSN leaked in reply: {reply[:200]}"
print("PASS: PII not present in assistant reply")
```

**Note:** This smoke test may produce false positives if the LLM generates its own fake-looking PII. The unit tests are the true source of confidence.

### 4.3 Manual Verification
1. Start backend + frontend.
2. Send: "My email is alice@example.com and phone is +1-650-555-1212."
3. Verify reply does not contain raw email/phone.
4. Trigger HITL 1 → proceed to production.
5. Check generated BRD: any `contact_email` or `stakeholder_phone` fields should have `<EMAIL_ADDRESS>` / `<PHONE_NUMBER>` if the LLM put PII there.
6. Open Phoenix (`http://localhost:6006`) and verify `guardrail.*` span attributes are present.

---

## 5. Exit Criteria

| # | Criterion | Verification |
|:---|:---|:---|
| 1 | `presidio-analyzer` and `presidio-anonymizer` are in `requirements.txt` | File review |
| 2 | `scan_and_protect()` masks SSN, CC, email, phone, bank, IBAN | Unit test `test_masks_credit_card`, `test_masks_ssn`, `test_masks_phone` |
| 3 | `scan_and_protect()` does NOT mask PERSON names | Unit test `test_logs_person_without_masking` |
| 4 | Guardrails are invoked at all 4 integration points | Code review of `invoke_llm.py`, `draft.py`, `schema_validate.py` |
| 5 | Telemetry spans carry `guardrail.*` attributes | Phoenix UI inspection |
| 6 | App starts successfully with Presidio installed | `uvicorn backend.main:app` smoke test |
| 7 | App starts successfully WITHOUT spaCy model (graceful fallback) | Uninstall `en_core_web_sm`, restart app, verify logs show fallback warning |

---

## 6. Risks & Mitigations

| Risk | Impact | Mitigation |
|:---|:---|:---|
| Presidio model download on first run | Low | Pre-download in `setup.ps1`; graceful fallback if missing |
| spaCy adds ~100MB dependency | Low | `spacy` is optional; regex recognizers work without it |
| Masking breaks JSON structure in BRD | Medium | Use `scan_dict_strings` (recursive field scan) instead of flat string scan |
| False positives mask legitimate data | Low | Only 6 entity types are masked; PERSON is log-only per locked decision |
| Performance: AnalyzerEngine per-call | Low | Engines are lazy singletons; analysis is fast for short texts (<5ms typical) |

---

## 7. Rollback Plan

If Presidio causes startup failures or compatibility issues:
1. Revert `backend/guardrails/presidio.py` to the stub (pass-through).
2. Remove imports from `invoke_llm.py`, `draft.py`, `schema_validate.py`.
3. App reverts to v2.0 Phase 2 behavior (no guardrails but fully functional).
