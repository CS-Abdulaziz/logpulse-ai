"""
ai_core/events/recorder.py — Convenience helpers that wrap emit_event().

Public API
----------
record(incident_id, event_type, node_name, message, metadata={}) → None
build_incident_timeline(incident_id) → str

Timeline format example
-----------------------
[10:31:12] INCIDENT_RECEIVED
[10:31:13] INCIDENT_CLASSIFIED → Memory / Critical
[10:31:13] CACHE_MISS
[10:31:14] RAG_RETRIEVED → Pod OOMKilled playbook
[10:31:15] RISK_ASSESSED → WARNING
[10:31:18] OPERATOR_APPROVED
[10:31:19] SANDBOX_EXECUTED → kubectl get pods (exit 0)
[10:31:20] WORKFLOW_COMPLETED
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from ai_core.events.event_bus import emit_event, get_events_for_incident
from ai_core.events.models import EventType, WorkflowEvent


# ---------------------------------------------------------------------------
# Core helper
# ---------------------------------------------------------------------------

def record(
    incident_id: str,
    event_type: EventType,
    node_name: str,
    message: str,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """
    Create and emit a WorkflowEvent in one call.

    Parameters
    ----------
    incident_id : str
        Usually ``state.trace_id`` — ties events to a specific pipeline run.
    event_type : EventType
        The kind of event (INCIDENT_CLASSIFIED, CACHE_HIT, …).
    node_name : str
        The LangGraph node that emitted this event.
    message : str
        Human-readable description of what happened.
    metadata : dict, optional
        Extra structured data (confidence scores, exit codes, …).
    """
    event = WorkflowEvent(
        incident_id=incident_id,
        event_type=event_type,
        node_name=node_name,
        message=message,
        metadata=metadata or {},
    )
    emit_event(event)


# ---------------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------------

def build_incident_timeline(incident_id: str) -> str:
    """
    Return a formatted timeline string for *incident_id*.

    Each line has the format::

        [HH:MM:SS] EVENT_TYPE → message

    The arrow and message are omitted for short/empty messages to keep
    the timeline readable.

    Events are sorted by their timestamp field (ISO-8601 strings sort
    chronologically without parsing).
    """
    events = get_events_for_incident(incident_id)
    if not events:
        return f"(no events recorded for incident {incident_id})"

    # Sort chronologically by timestamp string (ISO format sorts correctly)
    events = sorted(events, key=lambda e: e.timestamp)

    lines: list[str] = []
    for ev in events:
        # Parse ISO timestamp → HH:MM:SS
        try:
            dt = datetime.fromisoformat(ev.timestamp)
            time_str = dt.strftime("%H:%M:%S")
        except ValueError:
            time_str = ev.timestamp[:8]  # fallback: first 8 chars

        if ev.message:
            line = f"[{time_str}] {ev.event_type.value} → {ev.message}"
        else:
            line = f"[{time_str}] {ev.event_type.value}"

        lines.append(line)

    return "\n".join(lines)
