"""
tests/test_ingestion.py
========================
Unit tests for the LogPulse ingestion layer.

Tests
-----
filters.py
  1. keyword_filter passes an ERROR line
  2. keyword_filter blocks a plain INFO line
  3. keyword_filter is case-insensitive ("oomkilled" matches)
  4. dedup_filter passes the first occurrence of a log
  5. dedup_filter blocks a second occurrence of the same log

log_queue.py
  6. logs put into the queue are consumed in FIFO order
  7. print_stats shows correct received / analyzed / skipped counts

file_reader.py
  8. lines outside the timestamp range are skipped
  9. lines inside the range but with no trigger keyword are skipped

stream_reader.py
 10. scenario JSON loads correctly and emits the correct line count
     (time.sleep is mocked to avoid real delays)

Run:
    pytest tests/test_ingestion.py -v
    pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_core.ingestion.filters import TRIGGER_KEYWORDS, dedup_filter, keyword_filter
from ai_core.ingestion.log_queue import LogQueue
from ai_core.ingestion.file_reader import parse_timestamp, read_file_logs
from ai_core.ingestion.stream_reader import read_stream_mock


# ===========================================================================
# 1 — keyword_filter passes an ERROR line
# ===========================================================================

def test_keyword_filter_passes_error_line():
    line = "2025-01-15 22:14:07 ERROR database connection pool exhausted"
    assert keyword_filter(line) is True


# ===========================================================================
# 2 — keyword_filter blocks a plain INFO line
# ===========================================================================

def test_keyword_filter_blocks_info_line():
    line = "2025-01-15 22:00:01 INFO worker-api started successfully"
    assert keyword_filter(line) is False


# ===========================================================================
# 3 — keyword_filter is case-insensitive
# ===========================================================================

def test_keyword_filter_case_insensitive():
    # "oomkilled" should match the TRIGGER_KEYWORD "OOMKilled"
    assert keyword_filter("oomkilled: pod/worker exceeded memory") is True
    # Mixed case
    assert keyword_filter("EXCEPTION raised in handler thread") is True
    # All lower-case keyword from the list
    assert keyword_filter("connection timed out — timeout after 30s") is True


# ===========================================================================
# 4 — dedup_filter passes the first occurrence
# ===========================================================================

def test_dedup_filter_passes_first_occurrence():
    seen: set = set()
    line = "2025-01-15 22:14:07 ERROR OOMKilled: pod/worker-api exceeded 256Mi"
    assert dedup_filter(line, seen) is True
    assert len(seen) == 1


# ===========================================================================
# 5 — dedup_filter blocks a second occurrence of the same log
# ===========================================================================

def test_dedup_filter_blocks_duplicate():
    seen: set = set()
    line1 = "2025-01-15 22:14:07 ERROR OOMKilled: pod/worker-api exceeded 256Mi"
    # Same message, different timestamp — normalisation should make them equal
    line2 = "2025-01-15 22:15:01 ERROR OOMKilled: pod/worker-api exceeded 256Mi"

    assert dedup_filter(line1, seen) is True   # first: passes
    assert dedup_filter(line2, seen) is False  # duplicate: blocked


# ===========================================================================
# 6 — queue FIFO order
# ===========================================================================

def test_queue_fifo_order():
    """Logs put into the queue must be consumed in insertion order."""
    results: list[str] = []

    lq = LogQueue()
    lq.start_consumer(results.append)

    lq.put("log-A")
    lq.put("log-B")
    lq.put("log-C")
    lq.stop()

    assert results == ["log-A", "log-B", "log-C"]


# ===========================================================================
# 7 — print_stats shows correct received / analyzed / skipped
# ===========================================================================

def test_queue_print_stats(capsys):
    """After consuming 3 logs from a source of 10, stats must be correct."""
    lq = LogQueue()
    lq.start_consumer(lambda _: None)   # no-op consumer

    lq.put("ERROR: log-1")
    lq.put("ERROR: log-2")
    lq.put("ERROR: log-3")

    lq.set_received(10)   # 10 total lines from source, 3 passed filters
    lq.stop()
    lq.print_stats()

    out = capsys.readouterr().out
    assert "10" in out, "Expected 'received = 10' in stats output"
    assert "3"  in out, "Expected 'analyzed = 3' in stats output"
    assert "7"  in out, "Expected 'skipped = 7' in stats output"


# ===========================================================================
# 8 — file_reader skips lines outside the timestamp range
# ===========================================================================

def test_file_reader_skips_outside_range(tmp_path):
    """Lines whose timestamps fall outside [from_dt, to_dt] must be excluded."""
    log_file = tmp_path / "server.log"
    log_file.write_text(
        "2025-01-15 21:59:00 ERROR too early — before window\n"
        "2025-01-15 22:14:07 ERROR within range — should be queued\n"
        "2025-01-15 22:31:00 ERROR too late — after window\n",
        encoding="utf-8",
    )

    from_dt = datetime(2025, 1, 15, 22, 0, 0)
    to_dt   = datetime(2025, 1, 15, 22, 30, 0)
    seen: set = set()
    collected: list[str] = []

    read_file_logs(str(log_file), from_dt, to_dt, seen, collected.append)

    assert len(collected) == 1
    assert "within range" in collected[0]


# ===========================================================================
# 9 — file_reader skips lines that are in range but have no trigger keyword
# ===========================================================================

def test_file_reader_skips_no_keyword(tmp_path):
    """Lines with valid timestamps but no trigger keyword must be filtered."""
    log_file = tmp_path / "quiet.log"
    log_file.write_text(
        "2025-01-15 22:05:00 INFO  worker-api started normally\n"
        "2025-01-15 22:10:00 INFO  heartbeat OK, heap 64Mi/256Mi\n"
        "2025-01-15 22:15:00 WARN  memory usage 55%, within limits\n",
        encoding="utf-8",
    )

    from_dt = datetime(2025, 1, 15, 22, 0, 0)
    to_dt   = datetime(2025, 1, 15, 22, 30, 0)
    seen: set = set()
    collected: list[str] = []

    read_file_logs(str(log_file), from_dt, to_dt, seen, collected.append)

    assert len(collected) == 0, "No lines should pass when none have trigger keywords"


# ===========================================================================
# 10 — stream_reader loads scenario and emits correct line count
# ===========================================================================

def test_stream_mock_emits_correct_count(tmp_path):
    """
    Given a 4-entry scenario (1 INFO, 1 WARN, 1 ERROR, 1 FATAL):
    - INFO  → no trigger keyword → skipped
    - WARN  → no trigger keyword → skipped
    - ERROR → 'error' in line   → queued
    - FATAL → 'fatal' in line   → queued

    total=4, queued=2
    time.sleep is mocked to avoid real delays.
    """
    scenario = {
        "scenario": "test_scenario",
        "description": "Test scenario for unit tests",
        "logs": [
            {"delay": 0, "level": "INFO",  "message": "service started normally"},
            {"delay": 1, "level": "WARN",  "message": "memory usage rising: 65%"},
            {"delay": 1, "level": "ERROR", "message": "connection pool exhausted"},
            {"delay": 2, "level": "FATAL", "message": "OOMKilled: pod exceeded memory limit"},
        ],
    }
    scenario_path = tmp_path / "test_scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    seen: set = set()
    collected: list[str] = []

    with patch("time.sleep"):    # avoid real delays
        total, queued = read_stream_mock(str(scenario_path), seen, collected.append)

    assert total == 4, f"Expected 4 total events, got {total}"
    assert queued == 2, f"Expected 2 queued (ERROR + FATAL), got {queued}"
    assert len(collected) == 2
    # Both collected lines must contain a trigger-level word
    assert all(
        any(kw.lower() in line.lower() for kw in ["error", "fatal", "oomkilled"])
        for line in collected
    )
