"""
cache_node.py — LangGraph node functions for the Intelligent Cache Layer.

Nodes
-----
cache_check_node   — Lookup: hit → populate state.classification + duplicate_count
                              miss → return {} so the graph routes to classifier_node
cache_write_node   — Write: persist fresh classification after classifier_node
                            GUARD: skip if classification is None or source == "Fallback"

Edge function
-------------
route_after_cache  — "cache_hit" | "cache_miss"

Startup
-------
Call ``configure_cache(backend)`` once at application startup to swap from the
default InMemoryCacheBackend to SQLite (or any future backend) with no changes
to graph.py.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Path bootstrap — allow this module to be imported both when:
#   a) running from ai_core/workflow/  (python main.py)
#   b) running from the project root   (pytest)
# ---------------------------------------------------------------------------
_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.join(_CACHE_DIR, "..", "workflow")

for _p in (_CACHE_DIR, _WORKFLOW_DIR):
    _p = os.path.normpath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Internal imports (resolved via sys.path above)
# ---------------------------------------------------------------------------
from state import ClassificationData, LogState                  # noqa: E402
from agents.classifier_agent import normalize_severity          # noqa: E402
from cache_manager import (                                      # noqa: E402
    CacheBackend,
    CacheManager,
    CacheStats,
    InMemoryCacheBackend,
)

# ---------------------------------------------------------------------------
# Events wiring
# ---------------------------------------------------------------------------
_EVENTS_DIR = os.path.normpath(os.path.join(_CACHE_DIR, "..", "events"))
if _EVENTS_DIR not in sys.path:
    sys.path.insert(0, _EVENTS_DIR)
from recorder import record       # noqa: E402
from models import EventType      # noqa: E402

# ---------------------------------------------------------------------------
# Module-level backend — default is InMemory (zero dependencies, works offline)
# Swap with configure_cache() at application startup for persistence.
# ---------------------------------------------------------------------------
_backend: CacheBackend = InMemoryCacheBackend()
_manager: CacheManager = CacheManager(_backend)


def get_cache_stats() -> CacheStats:
    """Return a session-wide hit-rate snapshot from the active CacheManager."""
    return _manager.stats()


def configure_cache(backend: CacheBackend) -> None:
    """
    Replace the active cache backend at application startup.

    Call this *before* the LangGraph app is invoked.  Example::

        from cache.cache_node import configure_cache
        from cache.cache_manager import SqliteCacheBackend

        configure_cache(SqliteCacheBackend("data/logpulse_cache.db"))

    Parameters
    ----------
    backend:
        Any ``CacheBackend`` implementation (InMemory, SQLite, …).
    """
    global _backend, _manager
    _backend = backend
    _manager = CacheManager(backend)


# ---------------------------------------------------------------------------
# LangGraph nodes
# ---------------------------------------------------------------------------

def cache_check_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — cache lookup gate.

    Behaviour
    ---------
    **Cache HIT:**
        Rebuilds ``ClassificationData`` from the stored dict (using
        ``normalize_severity`` to restore the proper enum), sets
        ``duplicate_count`` to the current hit count, and returns both.
        The conditional edge ``route_after_cache`` will send the graph
        directly to ``orchestrator_node``, skipping the LLM call entirely.

    **Cache MISS:**
        Returns an empty dict — ``LogState.classification`` remains ``None``.
        ``route_after_cache`` routes to ``classifier_node``.
    """
    entry = _manager.lookup(state.raw_log)

    if entry is None:
        print("[Cache] MISS — routing to classifier_node.")
        record(
            incident_id=state.trace_id,
            event_type=EventType.CACHE_MISS,
            node_name="cache_check_node",
            message="Cache miss — routing to classifier",
        )
        return {}

    raw = entry.classification
    classification = ClassificationData(
        category=raw["category"],
        source=raw["source"],
        severity=normalize_severity(raw.get("severity", "")),
        summary=raw["summary"],
    )

    print(
        f"[Cache] HIT — pattern seen {entry.hit_count}x, "
        f"skipping LLM call. "
        f"(hash={entry.log_hash[:12]}…)"
    )
    record(
        incident_id=state.trace_id,
        event_type=EventType.CACHE_HIT,
        node_name="cache_check_node",
        message=f"Cache hit — seen {entry.hit_count}x",
        metadata={"hit_count": entry.hit_count, "log_hash": entry.log_hash[:12]},
    )

    return {
        "classification": classification,
        "duplicate_count": entry.hit_count,
    }


def cache_write_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — persist a fresh classification after ``classifier_node``.

    Guards
    ------
    * If ``state.classification`` is ``None`` → skip (classifier failed silently).
    * If ``state.classification.source == "Fallback"`` → skip (API was unreachable;
      caching a fallback would poison future lookups with a wrong classification).
    * If ``state.classification.category`` is ``"Unknown"`` or empty → skip
      (the model failed to produce a valid category; caching it would cause all
      future structurally-identical logs to hit a useless Unknown classification
      and never reach the real LLM).

    Returns an empty dict in all cases — this node is write-only and does not
    mutate any field in ``LogState``.
    """
    if state.classification is None:
        print("[Cache] SKIP write — classification is None.")
        return {}

    _source   = state.classification.source
    _category = state.classification.category.strip() if state.classification.category else ""

    if _source == "Fallback" or not _category or _category == "Unknown":
        print(
            "[Cache] SKIP WRITE — refusing to cache Fallback or Unknown "
            "classification targets."
        )
        return {}

    _manager.store(
        raw_log=state.raw_log,
        # mode="json" converts SeverityLevel enum → plain string ("Fatal", etc.)
        classification=state.classification.model_dump(mode="json"),
    )
    print(
        f"[Cache] WRITE — stored classification for pattern "
        f"(category={state.classification.category}, "
        f"severity={state.classification.severity.value})."
    )
    return {}


# ---------------------------------------------------------------------------
# Conditional edge function
# ---------------------------------------------------------------------------

def route_after_cache(state: LogState) -> str:
    """
    LangGraph conditional edge — called immediately after ``cache_check_node``.

    Returns
    -------
    "cache_hit"   → ``orchestrator_node``  (classification already populated)
    "cache_miss"  → ``classifier_node``    (LLM call required)
    """
    if state.classification is not None:
        return "cache_hit"
    return "cache_miss"
