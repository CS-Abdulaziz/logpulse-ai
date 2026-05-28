"""
tests/test_event_system.py
===========================
Unit tests for the LogPulse event system.

No network calls.  No Gemini API key required.

Tests
-----
1. event emission and retrieval
2. chronological ordering
3. incident_id filtering
4. timeline formatting
5. thread safety basic test
6. metadata persistence
7. reset_events() clears state

Run:
    pytest tests/test_event_system.py -v
    pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT       = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_EVENTS_DIR = os.path.join(_ROOT, "ai_core", "events")

if _EVENTS_DIR not in sys.path:
    sys.path.insert(0, _EVENTS_DIR)

from event_bus import (  # noqa: E402
    emit_event,
    get_all_events,
    get_events_for_incident,
    reset_events,
)
from models import EventType, WorkflowEvent   # noqa: E402
from recorder import build_incident_timeline, record  # noqa: E402

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_bus():
    """Reset the event bus before and after every test."""
    reset_events()
    yield
    reset_events()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(incident_id: str, event_type: EventType, message: str = "") -> WorkflowEvent:
    return WorkflowEvent(
        incident_id=incident_id,
        event_type=event_type,
        node_name="test_node",
        message=message or event_type.value,
    )


# ===========================================================================
# 1. Event emission and retrieval
# ===========================================================================

def test_emit_and_retrieve_event():
    """
    Emitting an event should make it retrievable via get_all_events()
    and get_events_for_incident().
    """
    ev = _make_event("inc-001", EventType.INCIDENT_RECEIVED, "test log")
    emit_event(ev)

    all_events = get_all_events()
    assert len(all_events) == 1
    assert all_events[0].event_id == ev.event_id
    assert all_events[0].message == "test log"

    by_incident = get_events_for_incident("inc-001")
    assert len(by_incident) == 1
    assert by_incident[0].event_id == ev.event_id


# ===========================================================================
# 2. Chronological ordering
# ===========================================================================

def test_chronological_ordering():
    """
    Events should be returned in insertion order.
    build_incident_timeline() should also present them chronologically.
    """
    types = [
        EventType.INCIDENT_RECEIVED,
        EventType.INCIDENT_CLASSIFIED,
        EventType.CACHE_MISS,
        EventType.WORKFLOW_COMPLETED,
    ]
    for t in types:
        emit_event(_make_event("inc-002", t))
        time.sleep(0.001)  # ensure distinct timestamps

    events = get_events_for_incident("inc-002")
    assert [e.event_type for e in events] == types

    timeline = build_incident_timeline("inc-002")
    lines = timeline.strip().splitlines()
    assert len(lines) == 4
    # Verify ordering: INCIDENT_RECEIVED must appear before WORKFLOW_COMPLETED
    first_line = lines[0]
    last_line = lines[-1]
    assert "INCIDENT_RECEIVED" in first_line
    assert "WORKFLOW_COMPLETED" in last_line


# ===========================================================================
# 3. Incident ID filtering
# ===========================================================================

def test_incident_id_filtering():
    """
    Events from different incidents must not bleed into each other's lists.
    """
    emit_event(_make_event("inc-A", EventType.CACHE_HIT))
    emit_event(_make_event("inc-B", EventType.CACHE_MISS))
    emit_event(_make_event("inc-A", EventType.WORKFLOW_COMPLETED))

    events_a = get_events_for_incident("inc-A")
    events_b = get_events_for_incident("inc-B")

    assert len(events_a) == 2
    assert all(e.incident_id == "inc-A" for e in events_a)
    assert len(events_b) == 1
    assert events_b[0].event_type == EventType.CACHE_MISS


# ===========================================================================
# 4. Timeline formatting
# ===========================================================================

def test_timeline_formatting():
    """
    build_incident_timeline() must produce one line per event in the expected
    [HH:MM:SS] TYPE → message format.
    """
    record("inc-004", EventType.INCIDENT_RECEIVED, "test_node", "Log arrived")
    record("inc-004", EventType.CACHE_MISS, "cache_check_node", "")
    record("inc-004", EventType.RAG_RETRIEVED, "rag_node", "Pod OOMKilled playbook")

    timeline = build_incident_timeline("inc-004")
    lines = timeline.strip().splitlines()

    assert len(lines) == 3
    # Line 0: [HH:MM:SS] INCIDENT_RECEIVED → Log arrived
    assert "INCIDENT_RECEIVED" in lines[0]
    assert "Log arrived" in lines[0]
    assert lines[0].startswith("[")
    # Line 1: CACHE_MISS — no message, no arrow
    assert "CACHE_MISS" in lines[1]
    # Line 2: RAG_RETRIEVED → Pod OOMKilled
    assert "RAG_RETRIEVED" in lines[2]
    assert "Pod OOMKilled" in lines[2]


def test_timeline_no_events_returns_placeholder():
    """An unknown incident_id should return a readable placeholder string."""
    result = build_incident_timeline("nonexistent-incident")
    assert "no events" in result.lower() or "nonexistent" in result


# ===========================================================================
# 5. Thread safety basic test
# ===========================================================================

def test_thread_safety():
    """
    50 threads each emitting 10 events concurrently.
    All 500 events must be retrievable without data corruption.
    """
    N_THREADS = 50
    EVENTS_PER_THREAD = 10
    incident_id = "inc-threads"

    def emit_batch():
        for _ in range(EVENTS_PER_THREAD):
            emit_event(_make_event(incident_id, EventType.RISK_ASSESSED))

    threads = [threading.Thread(target=emit_batch) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = get_events_for_incident(incident_id)
    assert len(events) == N_THREADS * EVENTS_PER_THREAD


# ===========================================================================
# 6. Metadata persistence
# ===========================================================================

def test_metadata_persistence():
    """
    Metadata dict stored in an event should be retrievable intact.
    """
    meta = {"confidence": 0.87, "category": "Memory", "tags": ["oom", "pod"]}
    ev = WorkflowEvent(
        incident_id="inc-006",
        event_type=EventType.ROOT_CAUSE_GENERATED,
        node_name="diagnostic_agent_node",
        message="OOM in worker",
        metadata=meta,
    )
    emit_event(ev)

    retrieved = get_events_for_incident("inc-006")
    assert len(retrieved) == 1
    assert retrieved[0].metadata["confidence"] == 0.87
    assert retrieved[0].metadata["category"] == "Memory"
    assert "oom" in retrieved[0].metadata["tags"]


# ===========================================================================
# 7. reset_events() clears state
# ===========================================================================

def test_reset_events_clears_state():
    """
    After reset_events(), get_all_events() must return an empty list and
    get_events_for_incident() must also return empty.
    """
    for _ in range(5):
        emit_event(_make_event("inc-007", EventType.INCIDENT_RECEIVED))

    assert len(get_all_events()) == 5

    reset_events()

    assert get_all_events() == []
    assert get_events_for_incident("inc-007") == []
