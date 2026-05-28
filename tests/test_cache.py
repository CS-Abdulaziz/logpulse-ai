"""
tests/test_cache.py
===================
Unit tests for the LogPulse Intelligent Cache Layer.

Run from project root:
    pytest tests/test_cache.py -v

Tests
-----
1. test_normalize_strips_timestamp        — single-log normalisation sanity check
2. test_same_hash_despite_dynamic_fields  — core guarantee: timestamp/PID/IP differences → same hash
3. test_distinct_logs_different_hash      — no false-positive collisions between different incidents
4. test_cache_miss_on_empty_backend       — fresh backend returns None
5. test_cache_hit_on_second_log           — second log (diff dynamic fields) is a HIT, hit_count bumped
6. test_store_is_idempotent               — calling store() twice does not corrupt hit_count
9. test_fallback_not_cached               — cache_write_node skips source=="Fallback"
10. test_unknown_category_not_cached       — cache_write_node skips category=="Unknown"
11. test_none_classification_not_cached    — cache_write_node skips None classification
12. test_sqlite_backend_persistence        — SqliteCacheBackend survives re-instantiation
"""

from __future__ import annotations

import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Resolve paths so the test works from the project root (pytest) *and* from
# inside ai_core/workflow/ (python -m pytest).
# ---------------------------------------------------------------------------
_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_CACHE_DIR = os.path.join(_ROOT, "ai_core", "cache")
_WORKFLOW_DIR = os.path.join(_ROOT, "ai_core", "workflow")

for _p in (_CACHE_DIR, _WORKFLOW_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from hashing import compute_log_hash, normalize_log          # noqa: E402
from cache_manager import (                                    # noqa: E402
    CacheManager,
    InMemoryCacheBackend,
    SqliteCacheBackend,
)
from cache_node import cache_write_node, configure_cache      # noqa: E402
from state import ClassificationData, LogState, SeverityLevel # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLASSIFICATION_DB = {
    "category": "DatabaseError",
    "source": "PostgreSQL",
    "severity": "Fatal",
    "summary": "Connection pool exhausted on postgres-db-01",
}

# Two real-looking logs — same incident, different dynamic fields
_LOG_A = (
    "[2026-05-26 14:32:01] ERROR [postgres-db-01] "
    "pid=1234 192.168.1.10:5432 "
    "FATAL: remaining connection slots are reserved for non-replication superuser"
)
_LOG_B = (
    "[2026-05-27 09:15:44] ERROR [postgres-db-01] "
    "pid=9999 10.0.0.22:5432 "
    "FATAL: remaining connection slots are reserved for non-replication superuser"
)

# A completely different incident
_LOG_OTHER = (
    "[2026-05-26 14:32:01] WARN [nginx-proxy-01] "
    "upstream timed out (110: connection timed out) while reading response header"
)


# ===========================================================================
# 1. Normalisation
# ===========================================================================

def test_normalize_strips_timestamp():
    raw = "[2026-05-26 14:32:01] ERROR pid=1234 FATAL: conn exhausted"
    result = normalize_log(raw)
    assert "<TIMESTAMP>" in result
    assert "2026" not in result
    assert "1234" not in result   # pid=1234 → <ID>  (4 digits — pid= prefix matched)


def test_normalize_strips_ip():
    raw = "connection from 192.168.100.200:5432 rejected"
    result = normalize_log(raw)
    assert "<IP>" in result
    assert "192" not in result


def test_normalize_strips_uuid():
    # Bare UUID (not inside a named key=value field) must become <UUID>.
    # Note: "trace_id=<uuid>" is consumed as <ID> by rule 6 (correct behaviour)
    # so this test uses a standalone UUID as it would appear in a raw log line.
    raw = "request 550e8400-e29b-41d4-a716-446655440000 failed with timeout"
    result = normalize_log(raw)
    assert "<UUID>" in result
    assert "550e8400" not in result


# ===========================================================================
# 2 & 3. Hash correctness
# ===========================================================================

def test_same_hash_despite_dynamic_fields():
    """
    Core guarantee: two logs describing the same incident but differing only in
    timestamp, PID, and IP must produce the identical SHA-256 hash.
    """
    hash_a = compute_log_hash(_LOG_A)
    hash_b = compute_log_hash(_LOG_B)
    assert hash_a == hash_b, (
        f"Expected identical hashes for semantically equivalent logs.\n"
        f"  norm(A) = {normalize_log(_LOG_A)}\n"
        f"  norm(B) = {normalize_log(_LOG_B)}"
    )


def test_distinct_logs_different_hash():
    """Semantically different incidents must NOT produce the same hash."""
    assert compute_log_hash(_LOG_A) != compute_log_hash(_LOG_OTHER)


# ===========================================================================
# 4. Cache miss
# ===========================================================================

def test_cache_miss_on_empty_backend():
    manager = CacheManager(InMemoryCacheBackend())
    result = manager.lookup(_LOG_A)
    assert result is None


# ===========================================================================
# 5. Cache hit — the key integration test
# ===========================================================================

def test_cache_hit_on_second_log():
    """
    Store LOG_A, then look up LOG_B (same pattern, different timestamp/PID/IP).
    Expect a cache HIT with hit_count == 2.
    """
    manager = CacheManager(InMemoryCacheBackend())

    # First occurrence — store
    manager.store(_LOG_A, _CLASSIFICATION_DB)
    assert manager.lookup.__doc__  # sanity: method exists

    # Second occurrence — different dynamic fields, same pattern
    entry = manager.lookup(_LOG_B)

    assert entry is not None, "Expected a cache HIT for LOG_B after storing LOG_A"
    assert entry.classification["category"] == "DatabaseError"
    assert entry.classification["severity"] == "Fatal"
    assert entry.hit_count == 2  # 1 (stored) + 1 (bumped on lookup)


# ===========================================================================
# 6. Idempotent store
# ===========================================================================

def test_store_is_idempotent():
    """Calling store() twice must not corrupt hit_count."""
    manager = CacheManager(InMemoryCacheBackend())

    manager.store(_LOG_A, _CLASSIFICATION_DB)
    manager.store(_LOG_A, _CLASSIFICATION_DB)   # second call should be a no-op

    entry = manager.lookup(_LOG_A)
    # lookup bumps to 2; a double-store must not have set hit_count > 1 before lookup
    assert entry is not None
    assert entry.hit_count == 2


# ===========================================================================
# 7 & 8. cache_write_node guards
# ===========================================================================

def test_fallback_not_cached():
    """
    cache_write_node must skip and return {} when source == 'Fallback',
    even if the category field itself is a valid non-Unknown value.
    This isolates the source guard from the category guard.
    """
    backend = InMemoryCacheBackend()
    configure_cache(backend)

    state = LogState(
        raw_log="[2026-05-26 14:32:01] ERROR something happened here now",
        classification=ClassificationData(
            category="Memory",          # valid category — guard fires on source only
            source="Fallback",
            severity=SeverityLevel.INFO,
            summary="Fallback classification triggered due to inference failure.",
        ),
    )

    result = cache_write_node(state)
    assert result == {}

    # Nothing must be written
    from hashing import compute_log_hash as _hash
    stored = backend.get(_hash(state.raw_log))
    assert stored is None, "Fallback source must never be written to cache"


# ===========================================================================
# 8a. Unknown category must not be cached  (Bug 2 regression guard)
# ===========================================================================

def test_unknown_category_not_cached():
    """
    cache_write_node must skip and return {} when category == 'Unknown',
    regardless of the source field.  This prevents cache poisoning when
    the remote model fails to produce a valid category.
    """
    backend = InMemoryCacheBackend()
    configure_cache(backend)

    state = LogState(
        raw_log="[2026-05-26 14:32:01] ERROR inference returned Unknown category",
        classification=ClassificationData(
            category="Unknown",          # triggers the new guard
            source="Remote",             # not Fallback — isolates category guard
            severity=SeverityLevel.WARNING,
            summary="Classifier returned Unknown category — inference degraded.",
        ),
    )

    result = cache_write_node(state)
    assert result == {}

    # Nothing must be written
    from hashing import compute_log_hash as _hash
    stored = backend.get(_hash(state.raw_log))
    assert stored is None, "Unknown category must never be written to cache"


def test_none_classification_not_cached():
    """cache_write_node must skip and return {} when classification is None."""
    backend = InMemoryCacheBackend()
    configure_cache(backend)

    state = LogState(
        raw_log="[2026-05-26 14:32:01] ERROR no classification set anywhere",
        classification=None,
    )

    result = cache_write_node(state)
    assert result == {}


# ===========================================================================
# 9. SQLite backend persistence
# ===========================================================================

def test_sqlite_backend_persistence():
    """
    Write via one SqliteCacheBackend instance, read via a second instance
    pointing at the same file — simulates a process restart.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        # --- Write ---
        manager_write = CacheManager(SqliteCacheBackend(db_path))
        manager_write.store(_LOG_A, _CLASSIFICATION_DB)

        # --- Read (fresh instance, same file) ---
        manager_read = CacheManager(SqliteCacheBackend(db_path))
        entry = manager_read.lookup(_LOG_B)   # same pattern as LOG_A

        assert entry is not None, "SQLite entry not found after re-opening DB"
        assert entry.classification["category"] == "DatabaseError"
        assert entry.hit_count == 2

    finally:
        # On Windows, SQLite WAL mode creates -wal and -shm sidecar files.
        # Use suppress so cleanup never masks a genuine assertion failure.
        import contextlib
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(db_path + suffix)


# ===========================================================================
# 10. CacheManager.stats() — hit-rate tracking
# ===========================================================================

def test_stats_correct_hit_rate():
    """
    Run a realistic mix of lookups through CacheManager and assert that
    stats() returns the correct totals, misses, and hit_rate.

    Scenario:
        - Store LOG_A pattern (1 unique incident).
        - lookup(LOG_A) → HIT          [total=1, hits=1]
        - lookup(LOG_B) → HIT (same pattern as A)  [total=2, hits=2]
        - lookup(LOG_OTHER) → MISS     [total=3, hits=2]
        - lookup(LOG_OTHER) → MISS (still not stored) [total=4, hits=2]

    Expected: total=4, hits=2, misses=2, hit_rate=0.5
    """
    manager = CacheManager(InMemoryCacheBackend())

    # Seed the cache with one pattern
    manager.store(_LOG_A, _CLASSIFICATION_DB)

    manager.lookup(_LOG_A)      # HIT  — same log
    manager.lookup(_LOG_B)      # HIT  — different timestamp/PID/IP, same pattern
    manager.lookup(_LOG_OTHER)  # MISS — different incident
    manager.lookup(_LOG_OTHER)  # MISS — still not stored

    s = manager.stats()

    assert s.total   == 4
    assert s.hits    == 2
    assert s.misses  == 2
    assert s.hit_rate == 0.5


def test_stats_zero_lookups_returns_zero_rate():
    """stats() must return 0.0 hit_rate (not crash) when no lookups have run."""
    manager = CacheManager(InMemoryCacheBackend())
    s = manager.stats()

    assert s.total    == 0
    assert s.hits     == 0
    assert s.misses   == 0
    assert s.hit_rate == 0.0


def test_stats_all_misses():
    """All misses → hit_rate == 0.0, misses == total."""
    manager = CacheManager(InMemoryCacheBackend())

    manager.lookup(_LOG_A)      # MISS
    manager.lookup(_LOG_OTHER)  # MISS

    s = manager.stats()
    assert s.hits     == 0
    assert s.misses   == 2
    assert s.hit_rate == 0.0


def test_stats_all_hits():
    """All hits → hit_rate == 1.0, misses == 0."""
    manager = CacheManager(InMemoryCacheBackend())

    manager.store(_LOG_A, _CLASSIFICATION_DB)

    manager.lookup(_LOG_A)   # HIT
    manager.lookup(_LOG_B)   # HIT (same pattern)

    s = manager.stats()
    assert s.hits     == 2
    assert s.misses   == 0
    assert s.hit_rate == 1.0
