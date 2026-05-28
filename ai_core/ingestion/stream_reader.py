"""
ai_core/ingestion/stream_reader.py

Reads a scenario JSON file and simulates a real-time log stream with
configurable inter-event delays.
No printing or user-input here — I/O stays in the caller (main.py / UI).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap — make filters importable
# ---------------------------------------------------------------------------
_INGESTION_DIR = str(Path(__file__).parent)
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

from filters import dedup_filter, keyword_filter  # noqa: E402


def read_stream_mock(
    scenario_path: str,
    seen: set,
    on_log: Callable[[str], None],
) -> Tuple[int, int]:
    """
    Emit log events from a scenario JSON file, simulating real-time delays.

    Scenario format
    ---------------
    .. code-block:: json

        {
          "scenario": "oom_critical",
          "description": "...",
          "logs": [
            {"delay": 0, "level": "INFO",  "message": "..."},
            {"delay": 2, "level": "ERROR", "message": "..."}
          ]
        }

    Processing per entry
    --------------------
    1. Sleep for ``entry["delay"]`` seconds.
    2. Build the full line: ``"{HH:MM:SS} {level} {message}"``.
    3. Print the line to the terminal.
    4. Apply ``keyword_filter`` → ``dedup_filter``.
    5. Print ``[LOGPULSE] analyzing...`` and call ``on_log(line)``
       for lines that pass both filters.

    Parameters
    ----------
    scenario_path : str
        Absolute or relative path to the scenario JSON file.
    seen : set
        Shared dedup hash set.
    on_log : callable
        Receives each qualifying log line (e.g. ``lq.put``).

    Returns
    -------
    (total_events, total_queued)
    """
    path = Path(scenario_path)
    with path.open(encoding="utf-8") as fh:
        scenario = json.load(fh)

    name = scenario.get("scenario", path.stem)
    logs = scenario.get("logs", [])
    total = len(logs)
    queued = 0

    print(f"[STREAM] scenario: {name} -- {total} events incoming...")

    for entry in logs:
        delay = entry.get("delay", 0)
        if delay > 0:
            time.sleep(delay)

        ts = datetime.now().strftime("%H:%M:%S")
        level = entry.get("level", "INFO")
        message = entry.get("message", "")
        line = f"{ts} {level} {message}"

        print(f"[{ts}] {level:<5} {message}")

        if not keyword_filter(line):
            continue

        if not dedup_filter(line, seen):
            continue

        print(f"           [LOGPULSE] analyzing...")
        on_log(line)
        queued += 1

    print("[STREAM] scenario complete")
    return total, queued
