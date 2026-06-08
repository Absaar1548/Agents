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
            IbanRecognizer,
            IpRecognizer,
            PhoneRecognizer,
            UsBankRecognizer,
            UsSsnRecognizer,
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
