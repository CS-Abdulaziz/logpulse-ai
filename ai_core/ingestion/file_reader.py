"""
ai_core/ingestion/file_reader.py

Reads a log file, applies timestamp range and content filters,
and passes qualifying lines to a caller-supplied callback.
No printing or user-input here — I/O stays in the caller (main.py / UI).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple

from ai_core.ingestion.filters import dedup_filter, keyword_filter

# ---------------------------------------------------------------------------
# Timestamp extraction
# ---------------------------------------------------------------------------
# Ordered from most-specific to least-specific so the bracketed variant
# is tried before the bare ISO variant (which is a substring of it).
_TS_PATTERNS = [
    (re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),     "%Y-%m-%dT%H:%M:%S"),
    (re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),      "%Y-%m-%d %H:%M:%S"),
]


def parse_timestamp(line: str) -> Optional[datetime]:
    """
    Extract a datetime from the beginning of *line*.

    Supported formats
    -----------------
    [2025-01-15 22:14:07]   bracketed ISO
     2025-01-15T22:14:07    ISO T-separated
     2025-01-15 22:14:07    ISO space-separated

    Returns None when no recognisable timestamp is found.
    Lines without a timestamp pass the timestamp filter by default
    (caller chooses whether to include them).
    """
    for pattern, fmt in _TS_PATTERNS:
        m = pattern.search(line)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None


def read_file_logs(
    filepath: str,
    from_dt: datetime,
    to_dt: datetime,
    seen: set,
    on_log: Callable[[str], None],
) -> Tuple[int, int]:
    """
    Read *filepath* line by line applying three filters in sequence:

    1. **Timestamp** — skip lines whose timestamp falls outside
       ``[from_dt, to_dt]``.  Lines with no parseable timestamp are
       included (cannot be excluded by time).
    2. **Keyword** — skip lines with no trigger keyword.
    3. **Dedup** — skip lines already seen (normalised-hash match).

    Calls ``on_log(line)`` for every line that passes all three filters.
    Prints progress every 100 lines and a one-line summary at the end.

    Parameters
    ----------
    filepath : str
        Path to the log file.
    from_dt, to_dt : datetime
        Inclusive time window.
    seen : set
        Shared dedup hash set (pass the same set across multiple calls
        to avoid cross-file duplicates).
    on_log : callable
        Receives each qualifying log line (e.g. ``lq.put``).

    Returns
    -------
    (total_lines_read, total_lines_queued)
    """
    path = Path(filepath)
    total = 0
    queued = 0

    print(f"[FILE] reading {path.name} "
          f"(from {from_dt:%H:%M} to {to_dt:%H:%M})...")

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            total += 1

            if total % 100 == 0:
                print(f"[FILE] processed {total} lines, {queued} queued...")

            # 1. Timestamp filter (lines without a timestamp are included)
            ts = parse_timestamp(line)
            if ts is not None and not (from_dt <= ts <= to_dt):
                continue

            # 2. Keyword filter
            if not keyword_filter(line):
                continue

            # 3. Dedup filter
            if not dedup_filter(line, seen):
                continue

            on_log(line)
            queued += 1

    print(f"[FILE] done -- {total} lines read, {queued} passed filters")
    return total, queued
