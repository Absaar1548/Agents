"""ArtifactStore — keeps large tool outputs out of the conversation buffer.

Pattern: a tool produces large content (a fetched template, a DB query
result, a doc dump). The full content gets stashed in this store. The
conversation buffer (LangGraph state.messages) only ever carries an
ArtifactRef containing a short summary + artifact_id. The assembler
dereferences refs at prompt-build time so the model still sees the
relevant artifact summary without inflating every subsequent turn with
the full payload.

This mirrors ws8_chassis's `ArtifactRef`/`ContextBundle` pattern. For the
PoC it's a process-local dict; production would back this with blob storage
(Azure Blob, S3) with the same get/put interface.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


ArtifactType = Literal["BRD_TEMPLATE", "UPLOADED_DOC", "GENERATED_DOC", "TOOL_OUTPUT"]


def _new_artifact_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactRef(BaseModel):
    """Reference to a large artifact stored elsewhere.

    Lives in the conversation buffer and in state.last_retrievals so the
    assembler can dereference it on subsequent turns.
    """

    artifact_id: str = Field(default_factory=_new_artifact_id)
    artifact_type: ArtifactType
    name: str
    summary: str
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


class ArtifactStore:
    """In-memory artifact store (dict[id] → content).

    Single process scope — matches the PoC's single-session model. To
    swap to persistent storage, replace `_content` reads/writes with
    Azure Blob / S3 / filesystem calls; ArtifactRef shape stays unchanged.
    """

    def __init__(self) -> None:
        self._content: dict[str, str] = {}
        self._refs: dict[str, ArtifactRef] = {}

    def put(
        self,
        *,
        artifact_type: ArtifactType,
        name: str,
        content: str,
        summary: str,
    ) -> ArtifactRef:
        ref = ArtifactRef(
            artifact_type=artifact_type,
            name=name,
            summary=summary,
            size_bytes=len(content.encode("utf-8")),
        )
        self._content[ref.artifact_id] = content
        self._refs[ref.artifact_id] = ref
        return ref

    def get_content(self, artifact_id: str) -> Optional[str]:
        return self._content.get(artifact_id)

    def get_ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        return self._refs.get(artifact_id)

    def count(self) -> int:
        return len(self._refs)
