"""Source-of-context protocols used by the ContextAssembler.

A "source" is anything the assembler can query for material that goes into
the prompt: document corpora (Vector DB), structured knowledge (KG),
artifact stores, future RAG indexes, etc.

Each source returns hits in a uniform shape (`RetrievedDoc`) so the
assembler doesn't care which backend produced them.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class RetrievedDoc(BaseModel):
    """One hit returned by a RetrievalSource.

    `id`         backend-specific document id (Chroma id, KG node id, etc.)
    `text`       the actual content the LLM should see (chunk text, glossary
                 entry, etc.). Kept short — the assembler will count tokens
                 and may drop hits if a budget is exceeded.
    `metadata`   free-form provenance (source name, doc title, section, etc.).
                 Surfaces via span attributes; goes into provenance dict.
    `distance`   optional similarity distance (lower = closer for cosine /
                 L2; None when not applicable, e.g. exact-match KG hits).
    """

    id: str
    text: str
    metadata: dict = Field(default_factory=dict)
    distance: Optional[float] = None


@runtime_checkable
class RetrievalSource(Protocol):
    """A source the assembler can query with a text string.

    Implementations: ChromaRetrievalSource (Phase 3),
    Neo4jKGSource (Phase 4 — different interface, but shares RetrievedDoc).
    """

    name: str  # short identifier used as span.attribute("source.kind")

    def retrieve(self, *, query: str, k: int = 4) -> list[RetrievedDoc]:
        ...
