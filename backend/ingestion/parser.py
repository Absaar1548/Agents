"""Document ingestion pipeline.

Supports text-only extraction per SOW §7:
  - Plain text (.txt) read directly.
  - PDF (.pdf) extracted via pymupdf (fitz) — no image/diagram parsing.
  - Generic binary / unknown formats rejected.

Chunking uses a simple character-based splitter with configurable overlap.
No LangChain splitter dependency — plain Python is sufficient for PoC.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

SUPPORTED_MIMETYPES = {
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}


def extract_text(file_path: str | Path, mime_type: str) -> str:
    """Extract full-text string from a file.

    Args:
        file_path: path to the uploaded temp file.
        mime_type: one of text/plain or application/pdf.

    Returns:
        Extracted text (may be empty for blank PDFs).
    Raises:
        ValueError: if mime_type is not supported.
        RuntimeError: if extraction fails.
    """
    if mime_type not in SUPPORTED_MIMETYPES:
        raise ValueError(
            f"Unsupported mime type: {mime_type!r}. "
            f"Supported: {sorted(SUPPORTED_MIMETYPES)}"
        )

    path = Path(file_path)

    if mime_type == "text/plain":
        return path.read_text(encoding="utf-8", errors="replace")

    if mime_type == "application/pdf":
        try:
            import fitz  # pymupdf
        except ImportError:
            raise RuntimeError("pymupdf is required for PDF extraction")
        doc = fitz.open(str(path))
        try:
            pages = [page.get_text() for page in doc]  # type: ignore[union-attr]
            return "\n\n".join(p.strip() for p in pages if p.strip())
        finally:
            doc.close()

    raise RuntimeError(f"Extraction not implemented for {mime_type}")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> list[str]:
    """Split text into overlapping chunks, respecting sentence boundaries.

    Uses a simple sentence-aware splitter:
      1. Split on sentence boundaries.
      2. Accumulate sentences until chunk_size is reached.
      3. Carry `overlap` characters (last sentences) into the next chunk.

    Args:
        text: the full extracted text.
        chunk_size: target character count per chunk.
        overlap: characters of context carried from previous chunk.

    Returns:
        List of chunk strings. Never empty — returns [""] for empty input.
    """
    if not text or not text.strip():
        return [""]

    sentences = _SENTENCE_BOUNDARY.split(text)
    if len(sentences) <= 1:
        # No clear sentence boundaries — fall back to fixed-size chunking
        return _fixed_size_chunks(text, chunk_size, overlap)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sent in sentences:
        sent_len = len(sent)
        if current_len + sent_len > chunk_size and current:
            # Emit current chunk
            chunks.append(" ".join(current))
            # Carry overlap: keep enough sentences to reach overlap chars
            overlap_chars = 0
            carry: list[str] = []
            for s in reversed(current):
                if overlap_chars >= overlap:
                    break
                carry.insert(0, s)
                overlap_chars += len(s)
            current = carry
            current_len = overlap_chars
        current.append(sent)
        current_len += sent_len

    if current:
        chunks.append(" ".join(current))

    return chunks if chunks else [text]


def _fixed_size_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Fallback: fixed-size character chunking when no sentence boundaries."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
