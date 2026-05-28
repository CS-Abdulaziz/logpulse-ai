"""
ai_core/ingestion/log_queue.py

Thread-safe bounded queue that feeds the analysis pipeline.
Standalone — no dependency on argparse or main.py internals.
A web UI can import and use LogQueue directly.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional


class LogQueue:
    """
    Thread-safe bounded queue with backpressure and ingestion statistics.

    *maxsize=50* — the producer blocks when the queue is full, which
    naturally throttles the log source when the pipeline is slow and
    prevents unbounded memory growth.

    Usage
    -----
    lq = LogQueue()
    lq.start_consumer(pipeline_fn)   # start background consumer
    lq.put("log line 1")             # blocks when queue is full
    lq.put("log line 2")
    lq.set_received(total_from_source)
    lq.stop()                        # send sentinel; wait for consumer
    lq.print_stats()
    """

    def __init__(self, maxsize: int = 50) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._consumer_thread: Optional[threading.Thread] = None
        self._stats: dict = {
            "received": 0,   # total lines from source (set via set_received)
            "analyzed": 0,   # lines successfully consumed by the pipeline
            "skipped": 0,    # computed as received - analyzed in print_stats
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_consumer(self, pipeline_fn: Callable[[str], None]) -> None:
        """Start the background thread that calls *pipeline_fn* per log."""

        def _consume() -> None:
            while True:
                item = self._queue.get()
                if item is None:           # sentinel — stop
                    self._queue.task_done()
                    break
                try:
                    pipeline_fn(item)
                    self._stats["analyzed"] += 1
                finally:
                    self._queue.task_done()

        self._consumer_thread = threading.Thread(target=_consume, daemon=True)
        self._consumer_thread.start()

    def put(self, log_line: str) -> None:
        """
        Enqueue *log_line*.

        Blocks when the queue is full (backpressure).
        Call this only for lines that have already passed all filters.
        """
        self._queue.put(log_line)

    def set_received(self, n: int) -> None:
        """
        Record the total number of lines the source produced before
        any filtering.  Call this before :meth:`print_stats`.
        """
        self._stats["received"] = n

    def stop(self) -> None:
        """Send the sentinel value and wait for the consumer to finish."""
        self._queue.put(None)
        if self._consumer_thread is not None:
            self._consumer_thread.join()

    def print_stats(self) -> None:
        """Print an ingestion summary after all processing is complete."""
        received = self._stats["received"] or self._stats["analyzed"]
        analyzed = self._stats["analyzed"]
        skipped = received - analyzed
        sep = "─" * 38
        print(sep)
        print("Ingestion summary")
        print(f"  Received  : {received} lines")
        print(f"  Analyzed  : {analyzed} (keyword match, unique)")
        print(f"  Skipped   : {skipped} (filtered or duplicate)")
        print(sep)
