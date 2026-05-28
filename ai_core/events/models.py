"""
ai_core/events/models.py — Domain models for the LogPulse event system.

Every significant transition in the LangGraph workflow emits a WorkflowEvent
that is stored in the in-process EventBus (event_bus.py).

Design
------
- EventType is a closed enum; add values here when new nodes are introduced.
- WorkflowEvent is a plain Pydantic model — no DB dependency.
- timestamp is stored as an ISO-8601 string so it serialises trivially.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict

from pydantic import BaseModel, Field


class EventType(str, Enum):
    INCIDENT_RECEIVED   = "INCIDENT_RECEIVED"
    INCIDENT_CLASSIFIED = "INCIDENT_CLASSIFIED"
    CACHE_HIT           = "CACHE_HIT"
    CACHE_MISS          = "CACHE_MISS"
    RAG_RETRIEVED       = "RAG_RETRIEVED"
    ROOT_CAUSE_GENERATED = "ROOT_CAUSE_GENERATED"
    SOLUTION_GENERATED  = "SOLUTION_GENERATED"
    RISK_ASSESSED       = "RISK_ASSESSED"
    OPERATOR_APPROVED   = "OPERATOR_APPROVED"
    OPERATOR_REJECTED   = "OPERATOR_REJECTED"
    SANDBOX_EXECUTED    = "SANDBOX_EXECUTED"
    SANDBOX_FAILED      = "SANDBOX_FAILED"
    INCIDENT_RECORDED   = "INCIDENT_RECORDED"
    WORKFLOW_COMPLETED  = "WORKFLOW_COMPLETED"


class WorkflowEvent(BaseModel):
    """Immutable record of one meaningful state transition in the workflow."""

    event_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    event_type:  EventType
    timestamp:   str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    node_name:   str
    message:     str
    metadata:    Dict[str, Any] = Field(default_factory=dict)
