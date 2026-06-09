"""Upload endpoint.

  POST /upload   multipart/form-data { file, session_id }   → { status, chunks, session_id }

Accepts plain-text (.txt) and PDF (.pdf) files. Extracts text, chunks,
and stores each chunk in Chroma with session-scoped metadata.

Text-only per SOW §7 — no image or diagram parsing.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from backend.ingestion.parser import (
    SUPPORTED_MIMETYPES,
    chunk_text,
    extract_text,
)

router = APIRouter()

# FastAPI's UploadFile already validates content_type from the multipart
# header, but clients can send incorrect MIME — we re-validate post-save.

_CHUNK_SIZE = 1000
_OVERLAP = 200


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(default_factory=lambda: uuid.uuid4().hex),
) -> dict:
    """Accept a document, extract text, chunk, and store in Chroma."""

    # Validate MIME type from the upload header
    mime = file.content_type or ""
    if mime not in SUPPORTED_MIMETYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type: {mime!r}. "
            f"Use text/plain or application/pdf.",
        )

    # Get the ChromaRetrievalSource from app state
    docs_source = getattr(request.app.state, "docs_source", None)
    if docs_source is None:
        raise HTTPException(
            status_code=500,
            detail="Document source (Chroma) not initialised — check server startup.",
        )

    # Read file bytes
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file uploaded.")

    # Write to a temp file for extraction (pymupdf needs a path)
    suffix = SUPPORTED_MIMETYPES.get(mime, ".bin")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        # Extract text
        text = extract_text(tmp_path, mime)
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="No extractable text found in file.")

        # Chunk
        chunks = chunk_text(text, chunk_size=_CHUNK_SIZE, overlap=_OVERLAP)

        # Store each chunk in Chroma with provenance metadata
        filename = file.filename or "unnamed"
        stored = 0
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            doc_id = f"{session_id}:{filename}:{i}"
            docs_source.add(
                doc_id=doc_id,
                text=chunk,
                metadata={
                    "source_file": filename,
                    "chunk_index": i,
                    "session_id": session_id,
                    "kind": "upload",
                },
            )
            stored += 1

        return {
            "status": "ingested",
            "chunks": stored,
            "session_id": session_id,
            "filename": filename,
        }

    finally:
        # Clean up temp file
        Path(tmp_path).unlink(missing_ok=True)
