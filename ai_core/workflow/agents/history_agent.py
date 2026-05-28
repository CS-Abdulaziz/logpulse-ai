"""
history_agent.py — Incident-history layer for the LogPulse AI pipeline.

Architecture
------------
HistoryBackend (ABC)
    ├── InMemoryHistoryBackend   — dict + threading.Lock  (dev / tests)
    └── SqliteHistoryBackend     — SQLite WAL, UPSERT, thread-safe  (production)

HistoryManager ties hashing → backend:
    get_context(raw_log)           — returns HistoryContext (first / recurrence)
    record_incident(raw_log, ...)  — INSERT first time; UPDATE on recurrence

DESIGN CONSTRAINT: read happens early in the graph (history_node); write happens
AFTER the workflow completes (call record_incident() from main.py / test harness).

Storage: data/incident_history.sqlite3  — separate from the cache DB.
Hashing: reuses compute_log_hash() from ai_core/cache/hashing.py so that
structurally-identical logs (same incident, different timestamps / IPs / PIDs)
always map to the same row — exactly the same guarantee the cache layer gives.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import warnings
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Path bootstrap — allow imports from ai_core/cache/ and ai_core/workflow/
# regardless of the working directory (pytest from project root, or
# python main.py from ai_core/workflow/).
# ---------------------------------------------------------------------------
_AGENTS_DIR  = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_CACHE_DIR   = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "cache"))

for _p in (_WORKFLOW_DIR, _CACHE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hashing import compute_log_hash          # noqa: E402  (from ai_core/cache/)
from state import HistoryContext, LogState    # noqa: E402  (from ai_core/workflow/)


# ---------------------------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------------------------

# HistoryContext is defined in state.py (to prevent circular imports) and
# re-exported from here for callers that import from history_agent directly.
# Importing it here makes it available as history_agent.HistoryContext.
__all__ = ["HistoryContext", "IncidentRecord", "HistoryBackend",
           "InMemoryHistoryBackend", "SqliteHistoryBackend",
           "HistoryManager", "history_node", "configure_history",
           "get_history_manager"]


class IncidentRecord(BaseModel):
    """A single stored row in the ``incident_history`` table."""

    hash: str
    """SHA-256 of the normalised log — primary key."""

    raw_log: str
    """The original raw log line (first occurrence)."""

    category: str
    source: str
    severity: str
    summary: str

    root_cause: Optional[str] = None
    applied_solution: Optional[str] = None
    outcome: Optional[str] = None
    """e.g. 'approved', 'rejected', 'simulated_success'"""

    occurrence_count: int = 1
    first_seen: datetime
    last_seen: datetime


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class HistoryBackend(ABC):
    """
    Interface every storage backend must implement.

    Keeping HistoryManager wired to this ABC means Redis / Postgres can be
    added later without touching graph.py or history_agent.py.
    """

    @abstractmethod
    def lookup(self, log_hash: str) -> Optional[IncidentRecord]:
        """Return the stored record for *log_hash*, or ``None`` if absent."""

    @abstractmethod
    def record(
        self,
        *,
        log_hash: str,
        raw_log: str,
        category: str,
        source: str,
        severity: str,
        summary: str,
        root_cause: Optional[str],
        applied_solution: Optional[str],
        outcome: Optional[str],
        now: datetime,
    ) -> None:
        """
        Persist a new incident or update an existing one.

        If *log_hash* already exists: increment ``occurrence_count``, refresh
        ``last_seen``, ``summary``, ``root_cause``, ``applied_solution``,
        ``outcome``.  ``first_seen`` and ``raw_log`` are NOT changed.

        If *log_hash* is new: INSERT with ``occurrence_count=1``,
        ``first_seen=last_seen=now``.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any resources held by the backend."""


# ---------------------------------------------------------------------------
# In-memory backend  (dev / tests)
# ---------------------------------------------------------------------------

class InMemoryHistoryBackend(HistoryBackend):
    """
    Thread-safe in-process dict store.

    Lifetime: process lifetime — cleared on restart.
    Use for local development, unit tests, and ephemeral pipelines.
    """

    def __init__(self) -> None:
        self._store: Dict[str, IncidentRecord] = {}
        self._lock = threading.Lock()

    def lookup(self, log_hash: str) -> Optional[IncidentRecord]:
        with self._lock:
            return self._store.get(log_hash)

    def record(
        self,
        *,
        log_hash: str,
        raw_log: str,
        category: str,
        source: str,
        severity: str,
        summary: str,
        root_cause: Optional[str],
        applied_solution: Optional[str],
        outcome: Optional[str],
        now: datetime,
    ) -> None:
        with self._lock:
            existing = self._store.get(log_hash)
            if existing is not None:
                updated = existing.model_copy(update={
                    "occurrence_count": existing.occurrence_count + 1,
                    "last_seen": now,
                    "summary": summary,
                    "root_cause": root_cause,
                    "applied_solution": applied_solution,
                    "outcome": outcome,
                })
                self._store[log_hash] = updated
            else:
                self._store[log_hash] = IncidentRecord(
                    hash=log_hash,
                    raw_log=raw_log,
                    category=category,
                    source=source,
                    severity=severity,
                    summary=summary,
                    root_cause=root_cause,
                    applied_solution=applied_solution,
                    outcome=outcome,
                    occurrence_count=1,
                    first_seen=now,
                    last_seen=now,
                )

    def close(self) -> None:
        pass  # Nothing to release for an in-memory store

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# SQLite backend  (production)
# ---------------------------------------------------------------------------

class SqliteHistoryBackend(HistoryBackend):
    """
    Persistent SQLite-backed history store — survives process restarts.

    Thread-safety: a ``threading.Lock`` serialises all writes; reads open a
    fresh connection so they do not block WAL-mode concurrent SELECTs.

    Schema (single table, ``hash`` is PRIMARY KEY)::

        hash             TEXT PRIMARY KEY
        raw_log          TEXT NOT NULL
        category         TEXT NOT NULL
        source           TEXT NOT NULL
        severity         TEXT NOT NULL
        summary          TEXT NOT NULL
        root_cause       TEXT
        applied_solution TEXT
        outcome          TEXT
        occurrence_count INTEGER NOT NULL DEFAULT 1
        first_seen       TEXT NOT NULL    -- ISO-8601 UTC
        last_seen        TEXT NOT NULL    -- ISO-8601 UTC

    On INSERT conflict (same hash): increment ``occurrence_count``, refresh
    ``last_seen`` and the mutable diagnostic fields.  ``first_seen`` and
    ``raw_log`` are preserved via ``excluded.*`` semantics — only the fields
    listed in the UPDATE clause are touched.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS incident_history (
            hash             TEXT    PRIMARY KEY,
            raw_log          TEXT    NOT NULL,
            category         TEXT    NOT NULL,
            source           TEXT    NOT NULL,
            severity         TEXT    NOT NULL,
            summary          TEXT    NOT NULL,
            root_cause       TEXT,
            applied_solution TEXT,
            outcome          TEXT,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            first_seen       TEXT    NOT NULL,
            last_seen        TEXT    NOT NULL
        )
    """

    # INSERT first occurrence; on conflict UPDATE the mutable columns.
    # first_seen and raw_log intentionally omitted from the UPDATE so they
    # keep their original values.
    _UPSERT = """
        INSERT INTO incident_history
            (hash, raw_log, category, source, severity, summary,
             root_cause, applied_solution, outcome,
             occurrence_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash) DO UPDATE SET
            occurrence_count = incident_history.occurrence_count + 1,
            last_seen        = excluded.last_seen,
            summary          = excluded.summary,
            root_cause       = excluded.root_cause,
            applied_solution = excluded.applied_solution,
            outcome          = excluded.outcome
    """

    def __init__(self, db_path: str = "data/incident_history.sqlite3") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        # Ensure the table exists on first connect
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self._DDL)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> IncidentRecord:
        return IncidentRecord(
            hash=row["hash"],
            raw_log=row["raw_log"],
            category=row["category"],
            source=row["source"],
            severity=row["severity"],
            summary=row["summary"],
            root_cause=row["root_cause"],
            applied_solution=row["applied_solution"],
            outcome=row["outcome"],
            occurrence_count=row["occurrence_count"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
        )

    # ------------------------------------------------------------------
    # HistoryBackend interface
    # ------------------------------------------------------------------

    def lookup(self, log_hash: str) -> Optional[IncidentRecord]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM incident_history WHERE hash = ?",
                (log_hash,),
            ).fetchone()
        finally:
            conn.close()

        return self._row_to_record(row) if row is not None else None

    def record(
        self,
        *,
        log_hash: str,
        raw_log: str,
        category: str,
        source: str,
        severity: str,
        summary: str,
        root_cause: Optional[str],
        applied_solution: Optional[str],
        outcome: Optional[str],
        now: datetime,
    ) -> None:
        now_iso = now.isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    self._UPSERT,
                    (
                        log_hash,
                        raw_log,
                        category,
                        source,
                        severity,
                        summary,
                        root_cause,
                        applied_solution,
                        outcome,
                        1,          # occurrence_count for INSERT; ignored on UPDATE
                        now_iso,    # first_seen  — preserved by UPDATE logic
                        now_iso,    # last_seen   — always refreshed
                    ),
                )
                conn.commit()
            finally:
                conn.close()  # Windows file-lock safety: release the handle immediately

    def close(self) -> None:
        pass  # Connections are opened/closed per-call; nothing to release here.


# ---------------------------------------------------------------------------
# HistoryManager — ties hashing → backend
# ---------------------------------------------------------------------------

class HistoryManager:
    """
    High-level history interface used by the LangGraph nodes.

    Wraps a ``HistoryBackend`` and owns the hashing logic so that neither
    the graph node nor the caller ever needs to know about normalisation
    or storage details.
    """

    def __init__(self, backend: HistoryBackend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_context(self, raw_log: str) -> HistoryContext:
        """
        Look up *raw_log* in the history store and return a ``HistoryContext``.

        First occurrence
        ----------------
        Returns ``HistoryContext(is_first_occurrence=True, occurrence_count=0)``
        with all ``previous_*`` fields set to ``None``.

        Recurrence
        ----------
        Returns ``HistoryContext(is_first_occurrence=False, ...)`` populated
        with the stored diagnostic fields and the recorded ``occurrence_count``
        (i.e. how many times *before* the current run).

        Parameters
        ----------
        raw_log:
            The original, unmodified log line from ``LogState.raw_log``.
        """
        log_hash = compute_log_hash(raw_log)
        record = self._backend.lookup(log_hash)

        if record is None:
            return HistoryContext(is_first_occurrence=True, occurrence_count=0)

        return HistoryContext(
            is_first_occurrence=False,
            occurrence_count=record.occurrence_count,
            previous_summary=record.summary,
            previous_root_cause=record.root_cause,
            previous_solution=record.applied_solution,
            previous_outcome=record.outcome,
            last_seen=record.last_seen,
        )

    def record_incident(
        self,
        raw_log: str,
        category: str,
        source: str,
        severity: str,
        summary: str,
        root_cause: Optional[str] = None,
        applied_solution: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> None:
        """
        Persist (or update) an incident record after the workflow completes.

        First occurrence
        ----------------
        Inserts a new row with ``occurrence_count=1`` and
        ``first_seen=last_seen=now``.

        Recurrence
        ----------
        Updates the existing row: increments ``occurrence_count``, refreshes
        ``last_seen`` and the mutable diagnostic columns (``summary``,
        ``root_cause``, ``applied_solution``, ``outcome``).  ``first_seen``
        and ``raw_log`` are preserved.

        Parameters
        ----------
        raw_log:
            The original, unmodified log line.
        category, source, severity, summary:
            Classification fields from ``ClassificationData``.
        root_cause:
            Diagnostic result (may be ``None`` if the diagnostic agent is a stub).
        applied_solution:
            Solution result as a human-readable string (may be ``None``).
        outcome:
            Human / simulated decision string, e.g. ``"simulated_success"``.
        """
        log_hash = compute_log_hash(raw_log)
        now = datetime.now(timezone.utc)
        self._backend.record(
            log_hash=log_hash,
            raw_log=raw_log,
            category=category,
            source=source,
            severity=severity,
            summary=summary,
            root_cause=root_cause,
            applied_solution=applied_solution,
            outcome=outcome,
            now=now,
        )


# ---------------------------------------------------------------------------
# Module-level singleton + configure hook  (mirrors cache_node.py)
# ---------------------------------------------------------------------------

# Default backend: InMemory (zero dependencies, works offline).
# Swap with configure_history() at application startup for SQLite persistence.
_backend: HistoryBackend = InMemoryHistoryBackend()
_manager: HistoryManager = HistoryManager(_backend)


def get_history_manager() -> HistoryManager:
    """Return the active module-level ``HistoryManager`` instance."""
    return _manager


def configure_history(backend: HistoryBackend) -> None:
    """
    Replace the active history backend at application startup (or in tests).

    Call this *before* the LangGraph app is invoked.  Example::

        from agents.history_agent import configure_history
        from agents.history_agent import SqliteHistoryBackend

        configure_history(SqliteHistoryBackend("data/incident_history.sqlite3"))

    Parameters
    ----------
    backend:
        Any ``HistoryBackend`` implementation (InMemory, SQLite, …).
    """
    global _backend, _manager
    _backend = backend
    _manager = HistoryManager(backend)


# ---------------------------------------------------------------------------
# LangGraph node function
# ---------------------------------------------------------------------------

def history_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — incident history lookup.

    Reads ``state.raw_log``, hashes it, and queries the history store.

    Returns
    -------
    dict
        ``{"history_context": HistoryContext}`` — merged into ``LogState``
        so downstream nodes (merge_context, diagnostic) can see past outcomes.

    Defensive behaviour
    -------------------
    Any exception (DB unavailable, corrupt record, …) is caught and logged.
    The node returns a safe first-occurrence context so the workflow never
    breaks — mirroring the RAG node's defensive philosophy.
    """
    try:
        context = _manager.get_context(state.raw_log)

        if context.is_first_occurrence:
            print("[History] First occurrence of this incident.")
        else:
            outcome_str = context.previous_outcome or "unknown"
            print(
                f"[History] Seen {context.occurrence_count} time(s) before. "
                f"Last outcome: {outcome_str}."
            )

        return {"history_context": context}

    except Exception as exc:
        warnings.warn(
            f"[History] WARNING: history lookup failed: {exc}. "
            "Returning safe first-occurrence context.",
            RuntimeWarning,
            stacklevel=2,
        )
        return {
            "history_context": HistoryContext(is_first_occurrence=True, occurrence_count=0)
        }
