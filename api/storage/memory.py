"""api/storage/memory.py — Thread-safe in-memory incident store.

Each incident record:
{
  "id":             str (UUID),
  "status":         IncidentStatus string,
  "created_at":     ISO-8601 string,
  "input_log":      str,
  "trace_id":       str | None,        # LogState.trace_id set after pipeline starts
  "classification": dict | None,
  "rag":            dict | None,
  "diagnosis":      dict | None,
  "solution":       dict | None,
  "risk":           dict | None,
  "execution":      dict | None,
  "report_path":    str | None,
  "duration_ms":    int | None,
  "state_snapshot": LogState | None,   # stored at awaiting_approval gate
}

SSE events are stored as a plain list that SSE handlers poll with a cursor.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Statuses from which an incident will never progress further
_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "rejected"})


class IncidentStore:
    def __init__(self) -> None:
        self._incidents:          Dict[str, dict]            = {}
        self._events:             Dict[str, List[dict]]      = {}
        self._approval_events:    Dict[str, threading.Event] = {}
        self._approval_decisions: Dict[str, Optional[str]]   = {}
        self._lock = threading.Lock()

    # ── Incident CRUD ─────────────────────────────────────────────────────────

    def create(self, incident_id: str, raw_log: str) -> dict:
        record = {
            "id":             incident_id,
            "status":         "queued",
            "created_at":     datetime.now(timezone.utc).isoformat(),
            "input_log":      raw_log,
            "trace_id":       None,
            "classification": None,
            "rag":            None,
            "diagnosis":      None,
            "solution":       None,
            "risk":           None,
            "execution":      None,
            "report_path":    None,
            "duration_ms":    None,
            "state_snapshot": None,
        }
        with self._lock:
            self._incidents[incident_id]          = record
            self._events[incident_id]             = []
            self._approval_events[incident_id]    = threading.Event()
            self._approval_decisions[incident_id] = None
        return record

    def update(self, incident_id: str, updates: Dict[str, Any]) -> None:
        with self._lock:
            if incident_id in self._incidents:
                self._incidents[incident_id].update(updates)

    def get(self, incident_id: str) -> Optional[dict]:
        with self._lock:
            return self._incidents.get(incident_id)

    def list_all(self) -> List[dict]:
        with self._lock:
            return sorted(
                [
                    {k: v for k, v in inc.items() if k != "state_snapshot"}
                    for inc in self._incidents.values()
                ],
                key=lambda x: x["created_at"],
                reverse=True,
            )

    # ── SSE events ────────────────────────────────────────────────────────────

    def append_event(self, incident_id: str, event_type: str, data: dict) -> None:
        with self._lock:
            if incident_id in self._events:
                self._events[incident_id].append({"type": event_type, "data": data})

    def get_events_since(self, incident_id: str, cursor: int) -> List[dict]:
        with self._lock:
            events = self._events.get(incident_id, [])
            return events[cursor:]

    # ── Approval gate ─────────────────────────────────────────────────────────

    def get_approval_event(self, incident_id: str) -> threading.Event:
        with self._lock:
            return self._approval_events[incident_id]

    def set_approval_decision(self, incident_id: str, decision: str) -> None:
        """Call from the HTTP handler thread; unblocks the pipeline thread."""
        with self._lock:
            self._approval_decisions[incident_id] = decision
        self._approval_events[incident_id].set()

    def get_approval_decision(self, incident_id: str) -> Optional[str]:
        with self._lock:
            return self._approval_decisions.get(incident_id)

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def is_terminal(self, incident_id: str) -> bool:
        """Return True if the incident has reached a final, immutable state."""
        with self._lock:
            return self._incidents.get(incident_id, {}).get("status") in _TERMINAL

    def purge_stale_queues(self) -> int:
        """
        Release approval-gate resources for every terminal incident.

        For non-terminal incidents whose approval_event is still waiting
        in a thread, the event object keeps the worker thread alive until
        its 300 s timeout.  We leave those alone — this method only frees
        data structures that are provably no longer needed.

        Returns the number of incidents cleaned up.
        """
        cleared = 0
        with self._lock:
            terminal_ids = [
                iid for iid, rec in self._incidents.items()
                if rec.get("status") in _TERMINAL
            ]
            for iid in terminal_ids:
                # Set the event before removing it — this unblocks any thread
                # that somehow still holds a reference and is blocking on wait().
                ev = self._approval_events.pop(iid, None)
                if ev is not None:
                    ev.set()
                self._approval_decisions.pop(iid, None)
                cleared += 1
        return cleared

    def purge_all(self) -> None:
        """
        Discard all in-memory state.

        Called once at server startup so that stale data from a previous
        uvicorn session (e.g. hot-reload) cannot interfere with fresh runs.
        All approval events are set before clearing so any lingering threads
        unblock immediately rather than waiting out their timeout.
        """
        with self._lock:
            for ev in self._approval_events.values():
                ev.set()
            self._incidents.clear()
            self._events.clear()
            self._approval_events.clear()
            self._approval_decisions.clear()

    # ── Serialisation helper ──────────────────────────────────────────────────

    def safe_dict(self, incident_id: str) -> Optional[dict]:
        """Return the incident dict without the non-serialisable state_snapshot."""
        with self._lock:
            inc = self._incidents.get(incident_id)
            if inc is None:
                return None
            return {k: v for k, v in inc.items() if k != "state_snapshot"}


# Module-level singleton shared by all routers
store = IncidentStore()
