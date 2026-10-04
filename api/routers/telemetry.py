"""api/routers/telemetry.py - Live telemetry SSE for Detection Theater."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ai_core.ingestion.stream_reader import read_stream_mock

router = APIRouter(tags=["telemetry"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCENARIOS_DIR = _PROJECT_ROOT / "data" / "scenarios"
_MAX_LOGS = 500
_GRID_COLS = 20
_GRID_ROWS = 14


@dataclass
class TelemetryMemoryStore:
    logs: Deque[dict] = field(default_factory=lambda: deque(maxlen=_MAX_LOGS))
    detection: Optional[dict] = None
    signatures: Deque[dict] = field(default_factory=lambda: deque(maxlen=20))
    error_counts: Counter = field(default_factory=Counter)
    heatmap: List[dict] = field(default_factory=list)
    latest_trigger_log: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "logs": list(self.logs),
                "detection": self.detection,
                "signatures": list(self.signatures),
                "error_concentration": _error_concentration(self.error_counts),
                "heatmap": self.heatmap or _base_heatmap(),
                "latest_trigger_log": self.latest_trigger_log,
            }

    def append_log(self, item: dict) -> None:
        with self._lock:
            self.logs.append(item)

    def set_detection(self, item: dict, trigger_log: str) -> None:
        with self._lock:
            self.detection = item
            self.latest_trigger_log = trigger_log

    def append_signature(self, item: dict) -> None:
        with self._lock:
            self.signatures.appendleft(item)

    def bump_service(self, service: str) -> None:
        with self._lock:
            self.error_counts[service] += 1

    def set_heatmap(self, cells: List[dict]) -> None:
        with self._lock:
            self.heatmap = cells


store = TelemetryMemoryStore()


@router.get("/telemetry/snapshot")
async def telemetry_snapshot() -> dict:
    return store.snapshot()


@router.get("/telemetry/stream")
async def stream_telemetry(
    scenario: str = Query(default="oom_critical"),
) -> StreamingResponse:
    return StreamingResponse(
        _telemetry_generator(scenario),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _telemetry_generator(scenario: str) -> AsyncGenerator[str, None]:
    out: "queue.Queue[Optional[tuple[str, dict]]]" = queue.Queue(maxsize=200)
    stop = threading.Event()

    thread = threading.Thread(
        target=_produce_scenario,
        args=(scenario, out, stop),
        daemon=True,
        name=f"telemetry-{scenario}",
    )
    thread.start()

    yield _sse("snapshot", store.snapshot())

    last_heartbeat = time.time()
    try:
        while True:
            try:
                item = await asyncio.wait_for(asyncio.to_thread(out.get), timeout=1.0)
            except asyncio.TimeoutError:
                if time.time() - last_heartbeat >= 15:
                    last_heartbeat = time.time()
                    yield _sse("heartbeat", {"ts": time.time()})
                if not thread.is_alive():
                    yield _sse("complete", {"scenario": scenario})
                    return
                continue

            if item is None:
                yield _sse("complete", {"scenario": scenario})
                return

            event_type, payload = item
            yield _sse(event_type, payload)
    finally:
        stop.set()


def _produce_scenario(
    scenario: str,
    out: "queue.Queue[Optional[tuple[str, dict]]]",
    stop: threading.Event,
) -> None:
    scenario_path = _SCENARIOS_DIR / f"{scenario}.json"
    if not scenario_path.exists():
        _put(out, ("telemetry_error", {"message": f"Scenario not found: {scenario}"}))
        _put(out, None)
        return

    seen: set = set()

    def on_event(event: dict) -> None:
        if stop.is_set():
            raise _TelemetryStopped()

        level = _normalize_level(str(event.get("level", "INFO")))
        message = str(event.get("message", ""))
        service = _service_from_message(message)
        ts = str(event.get("timestamp", ""))
        log_id = f"tel-{uuid.uuid4().hex[:12]}"
        line = str(event.get("line", f"{ts} {level} {message}"))

        log = {
            "id": log_id,
            "timestamp": ts,
            "level": level,
            "message": message,
            "service": service,
            "raw": line,
        }
        store.append_log(log)
        _put(out, ("log", log))

        if level in {"WARN", "ERR", "CRIT"}:
            store.bump_service(service)
            concentration = _error_concentration(store.error_counts)
            _put(out, ("error_concentration", {"items": concentration}))

        cells = _heatmap_for_message(message)
        store.set_heatmap(cells)
        _put(out, ("heatmap", {"cells": cells}))

    def on_log(line: str) -> None:
        detection = _detection_from_line(line)
        store.set_detection(detection, line)
        signature = {
            "id": detection["signature_id"],
            "label": detection["signature_label"],
            "minutesAgo": 0,
        }
        store.append_signature(signature)
        _put(out, ("detection_state", detection))
        _put(out, ("signature", signature))

    try:
        read_stream_mock(str(scenario_path), seen, on_log, on_event=on_event)
    except _TelemetryStopped:
        return
    except Exception as exc:
        _put(out, ("telemetry_error", {"message": str(exc)}))
    finally:
        _put(out, None)


class _TelemetryStopped(Exception):
    pass


def _put(
    out: "queue.Queue[Optional[tuple[str, dict]]]",
    item: Optional[tuple[str, dict]],
) -> None:
    try:
        out.put(item, timeout=1)
    except queue.Full:
        try:
            out.get_nowait()
        except queue.Empty:
            pass
        out.put(item, timeout=1)


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _normalize_level(level: str) -> str:
    upper = level.upper()
    if upper in {"FATAL", "CRITICAL", "CRIT"}:
        return "CRIT"
    if upper in {"ERROR", "ERR"}:
        return "ERR"
    if upper in {"WARN", "WARNING"}:
        return "WARN"
    return "INFO"


def _service_from_message(message: str) -> str:
    lower = message.lower()
    if "payment" in lower:
        return "payment-gw"
    if "worker-api" in lower:
        return "worker-api"
    if "auth" in lower:
        return "auth-service"
    if "replica" in lower or "db" in lower:
        return "db-replica-02"
    return "cluster-core"


def _detection_from_line(line: str) -> dict:
    lower = line.lower()
    if "oomkilled" in lower or "memory" in lower:
        confidence = 98.4 if "oomkilled" in lower else 95.0
        title = "Memory Exhaustion Cascade"
        summary = (
            "Backend ingestion detected memory pressure escalating toward an "
            "OOMKilled event. The event passed keyword and dedup filters and is "
            "ready for the incident workflow."
        )
        signature_id = "SIG-OOM"
        signature_label = "OOMKilled Memory Cascade"
        impact = "Revenue Crit."
    else:
        confidence = 91.0
        title = "Operational Anomaly"
        summary = "Backend ingestion accepted this log for workflow analysis."
        signature_id = "SIG-OPS"
        signature_label = "Operational Anomaly"
        impact = "Service Risk"

    return {
        "id": f"det-{uuid.uuid4().hex[:12]}",
        "title": title,
        "summary": summary,
        "confidence": confidence,
        "impact_area": impact,
        "status": "critical" if confidence >= 95 else "warning",
        "signature_id": signature_id,
        "signature_label": signature_label,
        "trigger_log": line,
        "scenario": "oom_critical",
        "detected_at": time.time(),
    }


def _base_heatmap() -> List[dict]:
    cells: List[dict] = []
    for row in range(_GRID_ROWS):
        for col in range(_GRID_COLS):
            seed = ((row + 1) * 17 + (col + 1) * 31) % 9
            cells.append({
                "id": f"cell-{row}-{col}",
                "row": row,
                "col": col,
                "intensity": 0.02 + (seed / 100),
                "state": "normal",
            })
    return cells


def _heatmap_for_message(message: str) -> List[dict]:
    cells = _base_heatmap()
    lower = message.lower()
    if "memory" not in lower and "oomkilled" not in lower:
        return cells

    for cell in cells:
        row = cell["row"]
        col = cell["col"]
        if row == 7 and 14 <= col <= 17:
            cell["intensity"] = 0.7 if "oomkilled" in lower else 0.45
            cell["state"] = "critical" if "oomkilled" in lower else "warning"
    return cells


def _error_concentration(counts: Counter) -> List[dict]:
    total = sum(counts.values())
    if total <= 0:
        return []
    return [
        {"service": service, "percent": round((count / total) * 100)}
        for service, count in counts.most_common(5)
    ]
