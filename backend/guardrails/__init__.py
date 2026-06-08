"""Guardrails module — PII/PCI scanning and protection."""
from __future__ import annotations

from backend.guardrails.presidio import scan_and_protect

__all__ = ["scan_and_protect"]
