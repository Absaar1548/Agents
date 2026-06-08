"""BRDResponse schema — the structured output produced by 'Generate BRD'.

Rebuilt fresh for the BRD agent project; intentionally not imported from
ws8_chassis. Shape mirrors ws8_chassis/agents/brd/schema.py minus the
BRDRequest-related fields (no inbound ticket in the agent flow).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NFRCategory(str, Enum):
    PERFORMANCE = "performance"
    SECURITY = "security"
    AVAILABILITY = "availability"
    SCALABILITY = "scalability"
    USABILITY = "usability"
    COMPLIANCE = "compliance"
    MAINTAINABILITY = "maintainability"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Stakeholder(BaseModel):
    name: str
    role: str
    interest: str


class FunctionalRequirement(BaseModel):
    id: str = Field(description="Identifier like FR-001")
    title: str
    description: str
    priority: Priority
    acceptance_criteria: list[str] = Field(min_length=1)


class NonFunctionalRequirement(BaseModel):
    id: str = Field(description="Identifier like NFR-001")
    category: NFRCategory
    description: str
    target: str = Field(description="Measurable target, e.g. 'P95 latency < 200ms'")


class Risk(BaseModel):
    description: str
    likelihood: Priority
    impact: Priority
    mitigation: str


class BRDResponse(BaseModel):
    brd_id: UUID = Field(default_factory=uuid4)
    title: str
    background: str
    objectives: list[str] = Field(min_length=1)
    stakeholders: list[Stakeholder] = Field(min_length=1)
    functional_requirements: list[FunctionalRequirement] = Field(min_length=1)
    non_functional_requirements: list[NonFunctionalRequirement] = Field(
        default_factory=list
    )
    acceptance_criteria: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    drafted_at: datetime = Field(default_factory=_utcnow)
    drafted_by: str = Field(description="agent_id@version")


# ---------------------------------------------------------------------------
# Draft versioning API responses
# ---------------------------------------------------------------------------

class DraftsListResponse(BaseModel):
    session_id: str
    versions: list[int]
    count: int
    latest_status: Optional[str] = None


class DraftDetailResponse(BaseModel):
    session_id: str
    entry: dict
    draft: BRDResponse
