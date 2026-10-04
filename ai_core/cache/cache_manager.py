"""
cache_manager.py — Backend-agnostic cache layer for log classifications.

Architecture
------------
CacheBackend (ABC)
    ├── InMemoryCacheBackend   — dict + threading.Lock  (default, dev/test)
    └── SqliteCacheBackend     — SQLite WAL, UPSERT, thread-safe  (production)

CacheManager ties hashing → backend:
    lookup(raw_log)           — returns CachedClassification on hit (bumps hit_count), else None
    store(raw_log, class_dict) — persists a fresh classification; no-op if already stored

DESIGN CONSTRAINT: only the *classification* dict is cached.
Remediation plans (RAG, diagnostic, solution) are never stored here.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel

from ai_core.cache.hashing import compute_log_hash


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class CachedClassification(BaseModel):
    """Immutable record stored for each unique (normalised) log pattern."""

    log_hash: str
    """SHA-256 of the normalised log — primary cache key."""

    classification: Dict[str, Any]
    """Serialised ClassificationData (category, source, severity, summary)."""

    hit_count: int = 1
    """How many times this pattern has been seen (1 = stored but never re-hit)."""

    first_seen: datetime
    """UTC timestamp of the first occurrence."""

    last_seen: datetime
    """UTC timestamp of the most recent occurrence (updated on every hit)."""


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class CacheBackend(ABC):
    """
    Interface every storage backend must implement.

    Keeping the graph wired to this ABC means Redis / Memcached / Postgres
    can be added later with zero changes to graph.py or cache_node.py.
    """

    @abstractmethod
    def get(self, log_hash: str) -> Optional[CachedClassification]:
        """Return the entry for *log_hash*, or ``None`` if absent."""

    @abstractmethod
    def set(self, entry: CachedClassification) -> None:
        """Persist (insert or replace) *entry*."""


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------

class InMemoryCacheBackend(CacheBackend):
    """
    Thread-safe in-process dict cache.

    Lifetime: process lifetime — cleared on restart.
    Use for local development, unit tests, and ephemeral pipelines.
    """

    def __init__(self) -> None:
        self._store: Dict[str, CachedClassification] = {}
        self._lock = threading.Lock()

    def get(self, log_hash: str) -> Optional[CachedClassification]:
        with self._lock:
            return self._store.get(log_hash)

    def set(self, entry: CachedClassification) -> None:
        with self._lock:
            self._store[entry.log_hash] = entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class SqliteCacheBackend(CacheBackend):
    """
    Persistent SQLite-backed cache — survives process restarts.

    Thread-safety: a ``threading.Lock`` serialises all writes; reads use
    WAL mode so concurrent SELECT calls do not block each other.

    Schema (single table, log_hash is PRIMARY KEY):
        log_hash        TEXT PRIMARY KEY
        classification  TEXT  (JSON-serialised dict)
        hit_count       INTEGER
        first_seen      TEXT  (ISO-8601 UTC)
        last_seen       TEXT  (ISO-8601 UTC)
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS classification_cache (
            log_hash       TEXT    PRIMARY KEY,
            classification TEXT    NOT NULL,
            hit_count      INTEGER NOT NULL DEFAULT 1,
            first_seen     TEXT    NOT NULL,
            last_seen      TEXT    NOT NULL
        )
    """

    _UPSERT = """
        INSERT INTO classification_cache
            (log_hash, classification, hit_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(log_hash) DO UPDATE SET
            hit_count  = excluded.hit_count,
            last_seen  = excluded.last_seen
    """

    def __init__(self, db_path: str = "logpulse_cache.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        # Create table on first connection
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

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    def get(self, log_hash: str) -> Optional[CachedClassification]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM classification_cache WHERE log_hash = ?",
                (log_hash,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        return CachedClassification(
            log_hash=row["log_hash"],
            classification=json.loads(row["classification"]),
            hit_count=row["hit_count"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
        )

    def set(self, entry: CachedClassification) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    self._UPSERT,
                    (
                        entry.log_hash,
                        json.dumps(entry.classification, ensure_ascii=False),
                        entry.hit_count,
                        entry.first_seen.isoformat(),
                        entry.last_seen.isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CacheStats:
    """Immutable snapshot of session-wide cache counters."""
    total: int
    hits: int
    misses: int
    hit_rate: float   # 0.0–1.0; 0.0 when total == 0 (no division-by-zero)


# ---------------------------------------------------------------------------
# Cache manager
# ---------------------------------------------------------------------------

class CacheManager:
    """
    High-level cache interface used by the LangGraph nodes.

    Wraps a ``CacheBackend`` and owns the hashing logic so that the nodes
    never need to know about hashing or serialisation details.

    Parameters
    ----------
    backend:
        Any ``CacheBackend`` implementation. Swap at startup to change storage.
    """

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend
        # Session-wide totals — in-process only, not persisted.
        # Python's GIL makes plain int increment safe for CPython; the
        # pipeline is single-threaded at the cache gate anyway.
        self._total_lookups: int = 0
        self._total_hits: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, raw_log: str) -> Optional[CachedClassification]:
        """
        Look up *raw_log* in the cache.

        On **hit**: increments ``hit_count`` and updates ``last_seen``,
        persists the change, and returns the updated entry.

        On **miss**: returns ``None`` — the caller must invoke the LLM.

        Parameters
        ----------
        raw_log:
            The raw, unmodified log line from ``LogState.raw_log``.
        """
        self._total_lookups += 1

        log_hash = compute_log_hash(raw_log)
        entry = self._backend.get(log_hash)

        if entry is None:
            return None

        self._total_hits += 1

        # Bump per-entry counters on every cache hit
        updated = entry.model_copy(update={
            "hit_count": entry.hit_count + 1,
            "last_seen": datetime.now(timezone.utc),
        })
        self._backend.set(updated)
        return updated

    def stats(self) -> CacheStats:
        """
        Return a snapshot of session-wide lookup counters.

        Returns
        -------
        CacheStats
            ``hit_rate`` is ``hits / total``, or ``0.0`` when no lookups
            have been made yet (guards against ZeroDivisionError).
        """
        total = self._total_lookups
        hits  = self._total_hits
        return CacheStats(
            total=total,
            hits=hits,
            misses=total - hits,
            hit_rate=hits / total if total > 0 else 0.0,
        )

    def store(self, raw_log: str, classification: Dict[str, Any]) -> None:
        """
        Persist a freshly computed *classification* for *raw_log*.

        No-op if this log pattern is already stored (``lookup`` handles
        subsequent hit-count bumps via UPSERT).

        Parameters
        ----------
        raw_log:
            The original log line (will be normalised + hashed internally).
        classification:
            A JSON-serialisable dict — typically ``ClassificationData
            .model_dump(mode="json")``.
        """
        log_hash = compute_log_hash(raw_log)

        if self._backend.get(log_hash) is not None:
            return  # already present; lookup() owns hit_count updates

        now = datetime.now(timezone.utc)
        entry = CachedClassification(
            log_hash=log_hash,
            classification=classification,
            hit_count=1,
            first_seen=now,
            last_seen=now,
        )
        self._backend.set(entry)
