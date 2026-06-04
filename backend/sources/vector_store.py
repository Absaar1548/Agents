"""Chroma-backed RetrievalSource for documents.

PersistentClient at `storage/chroma/` (file-based, no service). Uses
Chroma's default embedding function (ONNX MiniLM-L6-v2) — fully local,
~80 MB model downloaded once on first use. Plan footnote: swap to Azure
OpenAI embeddings later by passing an embedding_function to
get_or_create_collection.

The class is intentionally thin: assembler decides what to query and how
many hits to ask for; this class converts Chroma's response into our
uniform RetrievedDoc shape.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

from backend.sources.base import RetrievedDoc


_DEFAULT_PERSIST_DIR = (
    Path(__file__).resolve().parent.parent.parent / "storage" / "chroma"
)
_DEFAULT_COLLECTION = "brd_docs"


class ChromaRetrievalSource:
    name = "vector_store"

    def __init__(
        self,
        persist_dir: Optional[Path | str] = None,
        collection_name: str = _DEFAULT_COLLECTION,
    ):
        self.persist_dir = Path(persist_dir or os.environ.get(
            "CHROMA_PERSIST_DIR", _DEFAULT_PERSIST_DIR
        ))
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name

        self._client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        # Default embedding function — local, no API key required.
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    # ----- read path (used by the assembler) -----
    def retrieve(self, *, query: str, k: int = 4) -> list[RetrievedDoc]:
        if not query or not query.strip():
            return []
        result = self._collection.query(query_texts=[query], n_results=k)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        hits: list[RetrievedDoc] = []
        for i, (doc_id, text, meta, dist) in enumerate(
            zip(ids, docs, metas or [{}] * len(ids), dists or [None] * len(ids))
        ):
            hits.append(
                RetrievedDoc(
                    id=str(doc_id),
                    text=text or "",
                    metadata=meta or {},
                    distance=float(dist) if dist is not None else None,
                )
            )
        return hits

    # ----- write path (used by seed script) -----
    def add(self, *, doc_id: str, text: str, metadata: Optional[dict] = None) -> None:
        self._collection.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata or {}],
        )

    def count(self) -> int:
        return self._collection.count()

    def reset_collection(self) -> None:
        """Drop and recreate the collection — used by seed scripts for
        idempotent re-runs."""
        try:
            self._client.delete_collection(self.collection_name)
        except Exception:
            pass
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
