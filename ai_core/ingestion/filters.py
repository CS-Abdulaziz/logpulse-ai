"""
ai_core/ingestion/filters.py

Pure, stateless filtering functions for the log ingestion layer.
Both functions are side-effect free and safe to call from any context,
including a web UI that wants to pre-screen logs before queuing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Set

# ---------------------------------------------------------------------------
# Path bootstrap — make hashing.py importable (ai_core/cache/)
# ---------------------------------------------------------------------------
_CACHE_DIR = str(Path(__file__).parent.parent / "cache")
if _CACHE_DIR not in sys.path:
    sys.path.insert(0, _CACHE_DIR)

from hashing import compute_log_hash  # noqa: E402

# ---------------------------------------------------------------------------
# Trigger keywords — any match sends the line into the analysis pipeline
# ---------------------------------------------------------------------------
TRIGGER_KEYWORDS = [
    "ERROR",
    "FATAL",
    "CRITICAL",
    "OOMKilled",
    "Exception",
    "killed",
    "failed",
    "timeout",
    "unreachable",
    "connection refused",
    "out of memory",
]


def keyword_filter(line: str) -> bool:
    """
    Return True if *line* contains at least one trigger keyword.

    The check is case-insensitive so "oomkilled", "Failed", and
    "CONNECTION REFUSED" all match their respective keywords.
    """
    lower = line.lower()
    return any(kw.lower() in lower for kw in TRIGGER_KEYWORDS)


def dedup_filter(line: str, seen: Set[str]) -> bool:
    """
    Return True if *line* has not been seen before.

    Uses :func:`compute_log_hash` from the cache layer — the same
    normalisation that strips timestamps, PIDs, IPs, and other dynamic
    fields before hashing.  This means two log lines that describe the
    same incident but differ only in dynamic noise are treated as
    duplicates.

    Adds the hash to *seen* when returning True so the next identical
    line is correctly identified as a duplicate.
    """
    h = compute_log_hash(line)
    if h in seen:
        return False
    seen.add(h)
    return True
