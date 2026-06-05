"""
ai_core/events/event_bus.py — Thread-safe in-process event bus (singleton).

Architecture
------------
A module-level list (_events) guarded by a threading.Lock acts as the
in-process store.  All mutations go through the lock.

Public API
----------
emit_event(event)                   → None
get_events_for_incident(incident_id) → list[WorkflowEvent]
get_all_events()                    → list[WorkflowEvent]
reset_events()                      → None   ← required for test isolation
"""
from __future__ import annotations

import threading
from typing import List

from ai_core.events.models import WorkflowEvent

# ---------------------------------------------------------------------------
# Module-level singleton state — private
# ---------------------------------------------------------------------------
_events: List[WorkflowEvent] = []
_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit_event(event: WorkflowEvent) -> None:
    """Append *event* to the bus in a thread-safe manner."""
    with _lock:
        _events.append(event)


def get_events_for_incident(incident_id: str) -> List[WorkflowEvent]:
    """Return all events whose *incident_id* matches, in emission order."""
    with _lock:
        return [e for e in _events if e.incident_id == incident_id]


def get_all_events() -> List[WorkflowEvent]:
    """Return a snapshot of all events emitted so far."""
    with _lock:
        return list(_events)


def reset_events() -> None:
    """Clear ALL stored events.  Required between tests for isolation."""
    with _lock:
        _events.clear()
