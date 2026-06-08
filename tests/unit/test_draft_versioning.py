"""Unit tests for Phase 4 — Draft Versioning & Observability.

Covers:
  - schema_validate appending provenance to draft_history
  - review endpoints mutating the latest draft_history entry
  - drafting API endpoints listing / retrieving drafts
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.api.drafting import get_draft, list_drafts
from backend.api.review import _mutate_latest_draft_history
from backend.nodes.schema_validate import schema_validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runtime(model: str = "gpt-4o", provider: str = "azure.openai"):
    """Return a minimal AgentRuntime stand-in."""
    llm = SimpleNamespace(model=model, provider=provider)
    return SimpleNamespace(llm=llm)


def _make_config(thread_id: str = "test-session"):
    return {"configurable": {"thread_id": thread_id}}


def _valid_brd_dict() -> dict:
    return {
        "title": "Test BRD",
        "background": "We need a test system.",
        "objectives": ["Validate draft versioning"],
        "stakeholders": [
            {"name": "Alice", "role": "PM", "interest": "Delivery"}
        ],
        "functional_requirements": [
            {
                "id": "FR-001",
                "title": "Version tracking",
                "description": "Track draft versions",
                "priority": "high",
                "acceptance_criteria": ["History is append-only"],
            }
        ],
        "acceptance_criteria": ["All tests pass"],
        "drafted_by": "brd-agent@0.1.0",
    }


def _assembled_provenance(
    *,
    strategy: str = "BRD_GENERATION",
    prompt_hash: str = "abc123",
    prompt_id: str = "brd-drafting",
    prompt_version: str = "0.1.0",
) -> dict:
    return {
        "assembled": {
            "strategy": strategy,
            "prompt_template_hash": prompt_hash,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "token_accounting": {
                "system_prompt": 100,
                "memory": 50,
                "summary": 0,
                "draft_block": 0,
                "docs": 0,
                "kg": 0,
                "artifacts": 0,
                "conversation": 200,
                "total": 350,
            },
        },
        "draft_raw": json.dumps(_valid_brd_dict()),
    }


# ---------------------------------------------------------------------------
# schema_validate tests
# ---------------------------------------------------------------------------


class TestSchemaValidateHistory:
    def test_appends_history_on_success(self):
        state = {
            "last_retrievals": _assembled_provenance(),
            "draft_history": [],
        }
        runtime = _make_runtime()
        result = schema_validate(state, _make_config(), runtime)

        assert "draft_history" in result
        history = result["draft_history"]
        assert len(history) == 1
        entry = history[0]
        assert entry["version"] == 1
        assert entry["draft"]["title"] == "Test BRD"
        assert entry["context_strategy"] == "BRD_GENERATION"
        assert entry["prompt_hash"] == "sha256:abc123"
        assert entry["prompt_id"] == "brd-drafting"
        assert entry["prompt_version"] == "0.1.0"
        assert entry["model"] == "gpt-4o"
        assert entry["provider"] == "azure.openai"
        assert entry["token_accounting"]["total"] == 350
        assert entry["status"] == "draft"
        assert entry["reviewed_by"] is None
        assert entry["reviewed_at"] is None
        # produced_at should be a recent ISO timestamp
        assert datetime.fromisoformat(entry["produced_at"])

    def test_preserves_existing_history(self):
        existing = {
            "version": 1,
            "draft": _valid_brd_dict(),
            "produced_at": "2026-01-01T00:00:00+00:00",
            "prompt_hash": "sha256:old",
            "context_strategy": "BRD_GENERATION",
            "model": "gpt-4o",
            "provider": "azure.openai",
            "token_accounting": {},
            "status": "draft",
            "reviewed_by": None,
            "reviewed_at": None,
        }
        state = {
            "last_retrievals": _assembled_provenance(prompt_hash="def456"),
            "draft_history": [existing],
        }
        runtime = _make_runtime()
        result = schema_validate(state, _make_config(), runtime)

        history = result["draft_history"]
        assert len(history) == 2
        assert history[0]["version"] == 1
        assert history[1]["version"] == 2
        assert history[1]["prompt_hash"] == "sha256:def456"

    def test_failure_does_not_append(self):
        state = {
            "last_retrievals": {"draft_raw": "not-json"},
            "draft_history": [],
            "retry_count": 0,
        }
        runtime = _make_runtime()
        result = schema_validate(state, _make_config(), runtime)

        assert "draft_history" not in result
        assert result["retry_count"] == 1
        assert result["validation_errors"]


# ---------------------------------------------------------------------------
# review endpoint helper tests
# ---------------------------------------------------------------------------


class TestMutateLatestDraftHistory:
    def _make_graph(self):
        graph = MagicMock()
        return graph

    def test_approved(self):
        graph = self._make_graph()
        state = {
            "draft_history": [
                {
                    "version": 1,
                    "status": "draft",
                    "reviewed_by": None,
                    "reviewed_at": None,
                }
            ]
        }
        _mutate_latest_draft_history(
            graph, {"configurable": {"thread_id": "t1"}}, state, status="approved", reviewed_by="user"
        )
        updated = graph.update_state.call_args[0][1]["draft_history"]
        assert len(updated) == 1
        assert updated[0]["status"] == "approved"
        assert updated[0]["reviewed_by"] == "user"
        assert updated[0]["reviewed_at"]  # non-empty ISO string

    def test_rejected(self):
        graph = self._make_graph()
        state = {
            "draft_history": [
                {
                    "version": 1,
                    "status": "draft",
                    "reviewed_by": None,
                    "reviewed_at": None,
                }
            ]
        }
        _mutate_latest_draft_history(
            graph, {"configurable": {"thread_id": "t1"}}, state, status="rejected", reviewed_by="user"
        )
        updated = graph.update_state.call_args[0][1]["draft_history"]
        assert updated[0]["status"] == "rejected"
        assert updated[0]["reviewed_by"] == "user"

    def test_no_history_is_noop(self):
        graph = self._make_graph()
        state = {"draft_history": []}
        _mutate_latest_draft_history(
            graph, {"configurable": {"thread_id": "t1"}}, state, status="approved", reviewed_by="user"
        )
        graph.update_state.assert_not_called()


# ---------------------------------------------------------------------------
# drafting API endpoint tests
# ---------------------------------------------------------------------------


class TestDraftEndpoints:
    @patch("backend.api.drafting._read_graph_state")
    def test_list_drafts_empty(self, mock_read):
        mock_read.return_value = {"draft_history": []}
        req = MagicMock()
        resp = list_drafts("sess-1", req)
        assert resp.session_id == "sess-1"
        assert resp.versions == []
        assert resp.count == 0
        assert resp.latest_status is None

    @patch("backend.api.drafting._read_graph_state")
    def test_list_drafts_multiple(self, mock_read):
        mock_read.return_value = {
            "draft_history": [
                {"version": 1, "status": "approved"},
                {"version": 2, "status": "draft"},
            ]
        }
        req = MagicMock()
        resp = list_drafts("sess-2", req)
        assert resp.versions == [1, 2]
        assert resp.count == 2
        assert resp.latest_status == "draft"

    @patch("backend.api.drafting._read_graph_state")
    def test_get_draft_found(self, mock_read):
        mock_read.return_value = {
            "draft_history": [
                {
                    "version": 1,
                    "status": "draft",
                    "draft": _valid_brd_dict(),
                }
            ]
        }
        req = MagicMock()
        resp = get_draft(1, "sess-3", req)
        assert resp.session_id == "sess-3"
        assert resp.entry["version"] == 1
        assert resp.draft.title == "Test BRD"

    @patch("backend.api.drafting._read_graph_state")
    def test_get_draft_not_found(self, mock_read):
        mock_read.return_value = {"draft_history": []}
        req = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            get_draft(99, "sess-4", req)
        assert exc_info.value.status_code == 404
        assert "99" in exc_info.value.detail
