"""
tests/test_history_node.py
==========================
Unit tests for the LogPulse incident-history layer.

Run from project root:
    pytest tests/test_history_node.py -v

Tests
-----
1. test_first_occurrence_returns_correct_context
       get_context on an unseen log → is_first_occurrence=True, occurrence_count=0,
       all previous_* fields None.

2. test_recurrence_after_record_incident
       After record_incident, a second get_context on the *same* log returns
       is_first_occurrence=False, occurrence_count=1, and the stored previous_*
       fields match what was recorded.

3. test_hash_normalization_same_record
       Two logs that differ only in timestamp / IP / PID map to the *same* row
       (proves compute_log_hash is reused correctly).

4. test_upsert_increments_occurrence_count
       Recording the same hash twice yields occurrence_count=2 and exactly one
       row in the backend.

5. test_first_seen_preserved_last_seen_advances
       first_seen is kept from the first record_incident call; last_seen is
       refreshed on the second call.

6. test_defensive_node_returns_safe_context_on_backend_error
       history_node with a backend that raises returns a safe first-occurrence
       context rather than propagating the exception.

7. test_history_node_first_occurrence
       Full node invocation on a fresh backend → prints correct log and returns
       is_first_occurrence=True.

8. test_history_node_recurrence
       Full node invocation after a prior record → returns is_first_occurrence=False
       with the correct previous outcome.

9. test_sqlite_backend_persistence
       Write via one SqliteHistoryBackend instance, read via a second instance
       pointing at the same file (simulates process restart).

All tests use configure_history(InMemoryHistoryBackend()) or a temp-file
SQLite so they never touch the real data/incident_history.sqlite3.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from unittest import mock

from ai_core.workflow.agents import history_agent
from ai_core.workflow.agents.history_agent import (
    HistoryManager,
    HistoryBackend,
    InMemoryHistoryBackend,
    SqliteHistoryBackend,
    configure_history,
    get_history_manager,
    history_node,
)
from ai_core.workflow.state import HistoryContext, LogState


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

# Two real-looking Kubernetes logs — same incident, different timestamps/IPs/PIDs
_LOG_K8S_A = (
    "[2026-05-26 14:32:01] ERROR pod/api-gateway-7d8f9b5c6-xk2lp "
    "pid=1234 192.168.1.10 OOMKilled: container exceeded memory limit 512Mi; "
    "terminated by kernel out-of-memory killer"
)
_LOG_K8S_B = (
    "[2026-05-27 09:15:44] ERROR pod/api-gateway-7d8f9b5c6-xk2lp "
    "pid=9999 10.0.0.22 OOMKilled: container exceeded memory limit 512Mi; "
    "terminated by kernel out-of-memory killer"
)

# A completely different incident
_LOG_SSH = (
    "May 26 14:32:01 prod-bastion sshd[23841]: "
    "Failed password for invalid user admin from 203.0.113.42 port 51234 ssh2"
)

_RECORD_KWARGS = dict(
    raw_log=_LOG_K8S_A,
    category="Memory",
    source="Kubernetes",
    severity="Critical",
    summary="Container OOMKilled — exceeded 512Mi memory limit.",
    root_cause="Unbounded in-memory cache growth.",
    applied_solution="kubectl patch deployment api-gateway -p '{...}'",
    outcome="simulated_success",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_manager() -> HistoryManager:
    """Return a HistoryManager backed by a fresh InMemoryHistoryBackend."""
    return HistoryManager(InMemoryHistoryBackend())


# ===========================================================================
# 1. First occurrence
# ===========================================================================

def test_first_occurrence_returns_correct_context():
    """
    get_context on a log that has never been recorded must return:
      - is_first_occurrence = True
      - occurrence_count = 0
      - all previous_* fields = None
    """
    manager = _fresh_manager()
    ctx = manager.get_context(_LOG_K8S_A)

    assert ctx.is_first_occurrence is True
    assert ctx.occurrence_count == 0
    assert ctx.previous_summary is None
    assert ctx.previous_root_cause is None
    assert ctx.previous_solution is None
    assert ctx.previous_outcome is None
    assert ctx.last_seen is None


# ===========================================================================
# 2. Recurrence after record_incident
# ===========================================================================

def test_recurrence_after_record_incident():
    """
    After record_incident, a second get_context on the SAME log must return:
      - is_first_occurrence = False
      - occurrence_count = 1
      - previous_* fields populated from the record
    """
    manager = _fresh_manager()
    manager.record_incident(**_RECORD_KWARGS)

    ctx = manager.get_context(_LOG_K8S_A)

    assert ctx.is_first_occurrence is False
    assert ctx.occurrence_count == 1
    assert ctx.previous_summary == _RECORD_KWARGS["summary"]
    assert ctx.previous_root_cause == _RECORD_KWARGS["root_cause"]
    assert ctx.previous_solution == _RECORD_KWARGS["applied_solution"]
    assert ctx.previous_outcome == _RECORD_KWARGS["outcome"]
    assert ctx.last_seen is not None


# ===========================================================================
# 3. Hash normalisation — noise-varied logs map to the same record
# ===========================================================================

def test_hash_normalization_same_record():
    """
    _LOG_K8S_A and _LOG_K8S_B differ only in timestamp, PID, and IP.
    After recording with _LOG_K8S_A, get_context with _LOG_K8S_B must return
    is_first_occurrence=False — proving compute_log_hash is reused correctly.
    """
    manager = _fresh_manager()
    manager.record_incident(**_RECORD_KWARGS)   # record using LOG_K8S_A

    # Query using LOG_K8S_B (different timestamp / IP / PID → same hash)
    ctx = manager.get_context(_LOG_K8S_B)

    assert ctx.is_first_occurrence is False, (
        "LOG_K8S_B should be treated as a recurrence of LOG_K8S_A "
        "(same normalised hash), but got is_first_occurrence=True."
    )
    assert ctx.occurrence_count == 1


# ===========================================================================
# 4. UPSERT increments, doesn't duplicate
# ===========================================================================

def test_upsert_increments_occurrence_count():
    """
    Calling record_incident twice with the same (normalised) log must:
      - yield occurrence_count == 2 (not 1, not 3)
      - keep exactly one row in the backend
    """
    backend = InMemoryHistoryBackend()
    manager = HistoryManager(backend)

    manager.record_incident(**_RECORD_KWARGS)
    manager.record_incident(**_RECORD_KWARGS)

    ctx = manager.get_context(_LOG_K8S_A)
    assert ctx.occurrence_count == 2
    assert ctx.is_first_occurrence is False

    # Only one row in the backend dict
    assert len(backend) == 1


# ===========================================================================
# 5. first_seen preserved; last_seen advances
# ===========================================================================

def test_first_seen_preserved_last_seen_advances():
    """
    first_seen must keep the timestamp from the first record_incident call.
    last_seen must be updated (≥ first_seen) on the second call.
    """
    from ai_core.cache.hashing import compute_log_hash

    backend = InMemoryHistoryBackend()
    manager = HistoryManager(backend)

    manager.record_incident(**_RECORD_KWARGS)
    log_hash = compute_log_hash(_LOG_K8S_A)
    first_record = backend.lookup(log_hash)
    assert first_record is not None
    t1_first_seen = first_record.first_seen
    t1_last_seen  = first_record.last_seen

    manager.record_incident(**_RECORD_KWARGS)
    second_record = backend.lookup(log_hash)
    assert second_record is not None

    # first_seen must not change
    assert second_record.first_seen == t1_first_seen

    # last_seen must be >= the original last_seen (may be equal if clocks don't
    # advance between the two calls in a fast test, but must never go backwards)
    assert second_record.last_seen >= t1_last_seen


# ===========================================================================
# 6. Defensive node — backend error → safe context, no exception
# ===========================================================================

def test_defensive_node_returns_safe_context_on_backend_error():
    """
    If the backend raises on lookup, history_node must NOT propagate the
    exception.  It must return a safe first-occurrence HistoryContext.
    """
    class _BrokenBackend(HistoryBackend):
        def lookup(self, log_hash):
            raise RuntimeError("Simulated DB I/O failure")
        def record(self, **kwargs):
            raise RuntimeError("Simulated DB I/O failure")
        def close(self):
            pass

    configure_history(_BrokenBackend())

    state = LogState(
        raw_log="[2026-05-26 14:32:01] ERROR something happened in the system"
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = history_node(state)

    # Must have returned a dict with history_context
    assert "history_context" in result
    ctx = result["history_context"]
    assert ctx.is_first_occurrence is True
    assert ctx.occurrence_count == 0

    # Must have emitted a RuntimeWarning
    assert any(issubclass(w.category, RuntimeWarning) for w in caught), (
        "Expected a RuntimeWarning for the backend failure, got none."
    )

    # Restore clean backend for subsequent tests
    configure_history(InMemoryHistoryBackend())


# ===========================================================================
# 7. history_node — first occurrence (full node invocation)
# ===========================================================================

def test_history_node_first_occurrence(capsys):
    """
    history_node on a fresh backend must:
      - return {"history_context": HistoryContext(is_first_occurrence=True, ...)}
      - print a line containing "First occurrence"
    """
    configure_history(InMemoryHistoryBackend())

    state = LogState(
        raw_log="[2026-05-26 14:32:01] ERROR pod/api OOMKilled memory exceeded"
    )
    result = history_node(state)

    assert "history_context" in result
    ctx = result["history_context"]
    assert isinstance(ctx, HistoryContext)
    assert ctx.is_first_occurrence is True
    assert ctx.occurrence_count == 0

    captured = capsys.readouterr()
    assert "First occurrence" in captured.out


# ===========================================================================
# 8. history_node — recurrence (full node invocation)
# ===========================================================================

def test_history_node_recurrence(capsys):
    """
    After recording an incident, history_node must:
      - return is_first_occurrence=False
      - print a line mentioning the occurrence count and last outcome
    """
    backend = InMemoryHistoryBackend()
    configure_history(backend)

    # Seed the backend with a prior incident
    manager = get_history_manager()
    manager.record_incident(
        raw_log=_LOG_K8S_A,
        category="Memory",
        source="Kubernetes",
        severity="Critical",
        summary="OOMKilled.",
        root_cause="Memory leak.",
        applied_solution="Raised limit.",
        outcome="simulated_success",
    )

    # Now run the node with the same pattern (noise-varied log)
    state = LogState(raw_log=_LOG_K8S_B)
    result = history_node(state)

    assert "history_context" in result
    ctx = result["history_context"]
    assert ctx.is_first_occurrence is False
    assert ctx.occurrence_count == 1
    assert ctx.previous_outcome == "simulated_success"

    captured = capsys.readouterr()
    assert "Seen" in captured.out
    assert "simulated_success" in captured.out


# ===========================================================================
# 9. SQLite backend persistence across re-instantiation
# ===========================================================================

def test_sqlite_backend_persistence():
    """
    Write via one SqliteHistoryBackend instance, read via a second instance
    pointing at the same file — simulates a process restart.
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        # --- Write ---
        manager_write = HistoryManager(SqliteHistoryBackend(db_path))
        manager_write.record_incident(**_RECORD_KWARGS)

        # --- Read (fresh instance, same file) ---
        manager_read = HistoryManager(SqliteHistoryBackend(db_path))
        ctx = manager_read.get_context(_LOG_K8S_B)   # noise-varied log, same hash

        assert ctx.is_first_occurrence is False, (
            "SQLite entry not found after re-opening the DB file."
        )
        assert ctx.occurrence_count == 1
        assert ctx.previous_summary == _RECORD_KWARGS["summary"]
        assert ctx.previous_outcome == _RECORD_KWARGS["outcome"]

    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(db_path + suffix)
