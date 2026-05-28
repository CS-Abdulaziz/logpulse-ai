"""
hashing.py — Pure, stateless log normalisation and hashing.

No I/O, no side-effects. Safe to import and call from any context.

Design goal
-----------
Two log lines that describe the same incident but differ only in dynamic
fields (timestamp, PID, IP, request-ID, hex address …) must produce the
*same* SHA-256 digest so that the cache treats them as duplicates.

Rule ordering matters: more-specific patterns run first so they cannot
be partially consumed by a later, broader rule.
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Normalisation rules
# ---------------------------------------------------------------------------
# Each entry is (compiled_pattern, replacement_token).
# Rules are applied in sequence to a lowercased copy of the raw log.
# ---------------------------------------------------------------------------

_RULES: List[Tuple[re.Pattern[str], str]] = [

    # 1. ISO 8601 / RFC 3339 timestamps
    #    e.g.  2026-05-26T14:32:01.123Z   2026-05-26 14:32:01+05:30
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            re.IGNORECASE,
        ),
        "<TIMESTAMP>",
    ),

    # 2. Syslog / cron timestamps
    #    e.g.  May 26 14:32:01   Jan  3 08:00:00
    (
        re.compile(
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}",
            re.IGNORECASE,
        ),
        "<TIMESTAMP>",
    ),

    # 3. Bare wall-clock time (after full timestamps are gone)
    #    e.g.  14:32:01   09:15:44.007
    (
        re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),
        "<TIMESTAMP>",
    ),

    # 4. UUIDs (any variant)
    #    e.g.  550e8400-e29b-41d4-a716-446655440000
    (
        re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
            r"-[0-9a-f]{4}-[0-9a-f]{12}",
            re.IGNORECASE,
        ),
        "<UUID>",
    ),

    # 5. IPv4 addresses with optional port
    #    e.g.  192.168.1.10   10.0.0.1:5432
    (
        re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"),
        "<IP>",
    ),

    # 6. Named dynamic key=value / key:value fields
    #    e.g.  pid=1234  tid=5678  req_id=abc  request_id=xyz
    #          trace_id=abc123  span_id=0f4e  correlation-id=xyz
    (
        re.compile(
            r"\b(?:pid|tid|thread"
            r"|req(?:uest)?[-_]?id"
            r"|request[-_]?id"
            r"|correlation[-_]?id"
            r"|trace[-_]?id"
            r"|span[-_]?id"
            r")\s*[=:]\s*\S+",
            re.IGNORECASE,
        ),
        "<ID>",
    ),

    # 7. 0x-prefixed hex literals  (≥4 hex digits after 0x)
    #    e.g.  0xdeadbeef   0x1A2B3C4D
    (
        re.compile(r"\b0x[0-9a-f]{4,}\b", re.IGNORECASE),
        "<HEX>",
    ),

    # 8. Bare hex strings (≥8 consecutive hex chars, word-bounded)
    #    e.g.  deadbeef1234abcd   (kernel addresses, git SHAs, etc.)
    #    NOTE: runs after rule 7 so 0x-prefixed are already replaced.
    (
        re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE),
        "<HEX>",
    ),

    # 9. Long integers (≥5 digits) — port numbers, PIDs left unmatched above
    #    e.g.  12345   98765432   (but NOT  5432 which is a port width < 5)
    (
        re.compile(r"\b\d{5,}\b"),
        "<NUM>",
    ),

    # 10. Collapse all whitespace runs (newlines, tabs, multiple spaces)
    (
        re.compile(r"\s+"),
        " ",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_log(raw_log: str) -> str:
    """
    Normalise *raw_log* into a stable, noise-free string.

    Steps:
    1. Lowercase the entire string.
    2. Apply each substitution rule in order (see _RULES).
    3. Strip leading / trailing whitespace.

    Returns a string where all dynamic fields have been replaced with stable
    placeholder tokens, suitable for deterministic hashing.

    Examples
    --------
    >>> a = "[2026-05-26 14:32:01] ERROR pid=1234 192.168.1.1 FATAL: conn exhausted"
    >>> b = "[2026-05-27 09:15:44] ERROR pid=9999 10.0.0.22  FATAL: conn exhausted"
    >>> normalize_log(a) == normalize_log(b)
    True
    """
    text = raw_log.lower()
    for pattern, token in _RULES:
        text = pattern.sub(token, text)
    return text.strip()


def compute_log_hash(raw_log: str) -> str:
    """
    Return the SHA-256 hex digest of the normalised log.

    This is the canonical cache key consumed by ``CacheManager``.
    Two logs that differ only in dynamic noise will produce the same digest.

    Parameters
    ----------
    raw_log:
        The original, unmodified log line exactly as received by the pipeline.

    Returns
    -------
    str
        64-character lowercase hex string (SHA-256).
    """
    normalized = normalize_log(raw_log)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
