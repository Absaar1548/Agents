"""Upload endpoint stub — Phase 5 implementation.

Document ingestion pipeline with background chunking.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/upload", status_code=501)
def upload_document() -> dict:
    """Stub — Phase 5 will implement file upload + background chunking."""
    raise HTTPException(status_code=501, detail="Document ingestion not yet implemented.")
