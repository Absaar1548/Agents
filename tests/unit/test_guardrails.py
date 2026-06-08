"""Unit tests for Presidio guardrails.

These tests verify that PII detection and masking work correctly
without requiring an LLM or any external services.
"""
from __future__ import annotations

import pytest

from backend.guardrails.presidio import (
    _mask_messages,
    scan_and_protect,
    scan_dict_strings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cc_text():
    return "My card is 4111 1111 1111 1111 and email is alice@example.com"


@pytest.fixture
def ssn_text():
    return "SSN: 567-89-0123"


@pytest.fixture
def phone_text():
    return "Call me at +1-650-555-1212"


@pytest.fixture
def person_text():
    return "Contact John Smith for details"


# ---------------------------------------------------------------------------
# scan_and_protect
# ---------------------------------------------------------------------------

class TestScanAndProtect:
    def test_masks_credit_card_and_email(self, cc_text):
        cleaned, masked, logged = scan_and_protect(cc_text)
        assert "4111" not in cleaned
        assert "<CREDIT_CARD>" in cleaned
        assert "alice@example.com" not in cleaned
        assert "<EMAIL_ADDRESS>" in cleaned
        assert any(f["entity_type"] == "CREDIT_CARD" for f in masked)
        assert any(f["entity_type"] == "EMAIL_ADDRESS" for f in masked)

    def test_masks_ssn(self, ssn_text):
        cleaned, masked, logged = scan_and_protect(ssn_text)
        assert "567-89-0123" not in cleaned
        assert "<US_SSN>" in cleaned
        assert any(f["entity_type"] == "US_SSN" for f in masked)

    def test_masks_phone(self, phone_text):
        cleaned, masked, logged = scan_and_protect(phone_text)
        assert "650-555-1212" not in cleaned
        assert "<PHONE_NUMBER>" in cleaned
        assert any(f["entity_type"] == "PHONE_NUMBER" for f in masked)

    def test_logs_person_without_masking(self, person_text):
        cleaned, masked, logged = scan_and_protect(person_text)
        # "John Smith" should still be present (log-only per policy)
        assert "John Smith" in cleaned
        assert any(f["entity_type"] == "PERSON" for f in logged)
        assert not any(f["entity_type"] == "PERSON" for f in masked)

    def test_empty_text(self):
        cleaned, masked, logged = scan_and_protect("")
        assert cleaned == ""
        assert masked == []
        assert logged == []

    def test_no_entities(self):
        text = "Hello world, this is a safe message with no PII."
        cleaned, masked, logged = scan_and_protect(text)
        assert cleaned == text
        assert masked == []
        assert logged == []

    def test_findings_never_contain_original_text(self, cc_text):
        cleaned, masked, logged = scan_and_protect(cc_text)
        for finding in masked:
            assert "text" not in finding
            assert "entity_type" in finding
            assert "start" in finding
            assert "end" in finding
            assert "score" in finding

    def test_span_attributes(self, cc_text):
        # scan_and_protect opens a chat_span internally;
        # the call should complete without raising.
        cleaned, masked, logged = scan_and_protect(
            cc_text, session_id="test-session", scan_name="test.scan"
        )
        assert cleaned is not None


# ---------------------------------------------------------------------------
# _mask_messages
# ---------------------------------------------------------------------------

class TestMaskMessages:
    def test_masks_in_message_content(self):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My SSN is 567-89-0123"},
        ]
        cleaned, masked, logged = _mask_messages(msgs)
        assert cleaned[0]["content"] == "You are a helpful assistant."
        assert "<US_SSN>" in cleaned[1]["content"]
        assert any(f["entity_type"] == "US_SSN" for f in masked)

    def test_preserves_message_structure(self):
        msgs = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "User message with email alice@example.com"},
            {"role": "assistant", "content": "Assistant reply."},
        ]
        cleaned, masked, logged = _mask_messages(msgs)
        assert cleaned[0]["role"] == "system"
        assert cleaned[1]["role"] == "user"
        assert cleaned[2]["role"] == "assistant"
        assert "alice@example.com" not in cleaned[1]["content"]
        assert "<EMAIL_ADDRESS>" in cleaned[1]["content"]
        assert cleaned[2]["content"] == "Assistant reply."

    def test_empty_content_messages(self):
        msgs = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Hello"},
        ]
        cleaned, masked, logged = _mask_messages(msgs)
        assert cleaned[0]["content"] == ""
        assert cleaned[1]["content"] == "Hello"


# ---------------------------------------------------------------------------
# scan_dict_strings
# ---------------------------------------------------------------------------

class TestScanDictStrings:
    def test_scans_nested_strings(self):
        data = {
            "title": "Project Alpha",
            "contact": "Email alice@example.com",
            "nested": {"phone": "+1-650-555-1212"},
            "items": ["SSN 567-89-0123", "safe text"],
            "number": 42,
        }
        cleaned, masked, logged = scan_dict_strings(data)
        assert "alice@example.com" not in cleaned["contact"]
        assert "<EMAIL_ADDRESS>" in cleaned["contact"]
        assert "+1-650-555-1212" not in cleaned["nested"]["phone"]
        assert "<PHONE_NUMBER>" in cleaned["nested"]["phone"]
        assert "567-89-0123" not in cleaned["items"][0]
        assert "<US_SSN>" in cleaned["items"][0]
        assert cleaned["items"][1] == "safe text"
        assert cleaned["number"] == 42
        assert cleaned["title"] == "Project Alpha"

    def test_preserves_dict_structure(self):
        data = {
            "a": {"b": {"c": "email alice@example.com"}},
        }
        cleaned, masked, logged = scan_dict_strings(data)
        assert cleaned["a"]["b"]["c"] == "email <EMAIL_ADDRESS>"

    def test_empty_dict(self):
        cleaned, masked, logged = scan_dict_strings({})
        assert cleaned == {}
        assert masked == []
        assert logged == []
