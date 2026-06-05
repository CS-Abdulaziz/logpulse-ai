"""api/routers/incidents.py — Incident endpoints + SSE stream."""
from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from api.adapters.pipeline  import PipelineAdapter, _REPORTS_DIR
from api.models.schemas     import (
    ApproveResponse,
    IncidentResponse,
    IncidentStatus,
    SubmitLogRequest,
)
from api.storage.memory import store

router    = APIRouter(tags=["incidents"])
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pipeline")

# How long (in 0.3 s ticks) between SSE keepalive comments sent to the browser.
# 100 ticks × 0.3 s = 30 s  (keeps proxies from closing idle connections)
_KEEPALIVE_TICKS = 100

# Close the SSE stream after this many consecutive empty polls with no events.
# 2 000 ticks × 0.3 s ≈ 10 minutes of inactivity.
_MAX_IDLE_TICKS = 2000


# ── POST /api/incidents ───────────────────────────────────────────────────────

@router.post("/incidents", response_model=IncidentResponse, status_code=202)
async def submit_incident(body: SubmitLogRequest) -> dict:
    """
    Submit a raw log for analysis.
    Returns immediately with ``status=queued`` and the incident id.
    The pipeline runs in the background; track progress via the SSE stream.
    """
    # Free approval-gate threading.Events from completed previous incidents so
    # those worker threads are not holding pool slots unnecessarily.
    store.purge_stale_queues()

    incident_id = str(uuid.uuid4())
    record = store.create(incident_id, body.log)

    adapter = PipelineAdapter(incident_id, store)
    # get_running_loop() is the correct call inside an async context (Python 3.10+).
    asyncio.get_running_loop().run_in_executor(
        _executor, adapter.run, body.log, body.scenario or "default"
    )

    return {**record, "status": IncidentStatus.queued}


# ── GET /api/incidents ────────────────────────────────────────────────────────

@router.get("/incidents", response_model=list[IncidentResponse])
async def list_incidents() -> list:
    """Return all incidents, newest first."""
    return store.list_all()


# ── GET /api/incidents/{id} ───────────────────────────────────────────────────

@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: str) -> dict:
    record = store.safe_dict(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return record


# ── GET /api/incidents/{id}/stream  (SSE) ─────────────────────────────────────

@router.get("/incidents/{incident_id}/stream")
async def stream_incident(incident_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream for a single incident.

    Event types emitted:
      stage             — {"name": str, "status": "running"|"done"|"error", ...}
      awaiting_approval — {"commands": [...], "risk_level": str}
      command_result    — {"command": str, "success": bool, "stdout": str, ...}
      complete          — {"incident_id": str, "duration_ms": int}

    If the incident has already finished when the client connects, all stored
    events are replayed immediately and the stream closes — no polling needed.
    """
    if store.get(incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    return StreamingResponse(
        _sse_generator(incident_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


async def _sse_generator(incident_id: str) -> AsyncGenerator[str, None]:
    """
    Poll the event list and yield SSE-formatted strings.

    Two paths:
    1. Incident already terminal when we connect → replay all stored events
       and return immediately (handles the fast-pipeline case where all events
       are written before the browser opens the stream).
    2. Incident still in progress → poll every 0.3 s, emit events as they
       arrive, send keepalive comments every 30 s, exit on terminal event or
       after 10 minutes of silence.
    """
    # ── Path 1: incident already done ────────────────────────────────────────
    if store.is_terminal(incident_id):
        stored = store.get_events_since(incident_id, 0)
        for event in stored:
            yield _sse(event["type"], event["data"])
        return

    # ── Path 2: pipeline still running ───────────────────────────────────────
    # Initial heartbeat confirms the connection is alive before the first stage.
    yield _sse("heartbeat", {"incident_id": incident_id})

    cursor     = 0
    idle_ticks = 0

    while True:
        events = store.get_events_since(incident_id, cursor)

        if events:
            idle_ticks = 0
            for event in events:
                cursor += 1
                yield _sse(event["type"], event["data"])
                # Terminal event — pipeline is done, close the stream.
                if event["type"] in ("complete", "error"):
                    return

        else:
            idle_ticks += 1

            # Keepalive comment every 30 s so proxies/browsers don't time out.
            if idle_ticks % _KEEPALIVE_TICKS == 0:
                yield ": keepalive\n\n"

            # If the pipeline has been silent for 10 minutes, give up.
            if idle_ticks >= _MAX_IDLE_TICKS:
                yield ": timeout — closing stream after 10 min of inactivity\n\n"
                return

            # Re-check path 1: the incident may have become terminal between
            # polls (race between pipeline thread and SSE generator startup).
            if store.is_terminal(incident_id):
                stored = store.get_events_since(incident_id, cursor)
                for event in stored:
                    yield _sse(event["type"], event["data"])
                return

        await asyncio.sleep(0.3)


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ── POST /api/incidents/{id}/approve ─────────────────────────────────────────

@router.post("/incidents/{incident_id}/approve", response_model=ApproveResponse)
async def approve_incident(incident_id: str) -> dict:
    record = store.get(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if record["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Incident is not awaiting approval (current status: {record['status']})",
        )

    store.set_approval_decision(incident_id, "approve")

    return {"status": "approved", "incident_id": incident_id}


# ── POST /api/incidents/{id}/reject ──────────────────────────────────────────

@router.post("/incidents/{incident_id}/reject")
async def reject_incident(incident_id: str) -> dict:
    record = store.get(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if record["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Incident is not awaiting approval (current status: {record['status']})",
        )

    store.set_approval_decision(incident_id, "reject")

    return {"status": "rejected", "incident_id": incident_id}


# ── GET /api/incidents/{id}/report ───────────────────────────────────────────

@router.get("/incidents/{incident_id}/report")
async def get_report(incident_id: str) -> FileResponse:
    """Return the generated PDF report for this incident."""
    record = store.safe_dict(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Try the stored report_path first
    report_path = record.get("report_path")
    if report_path and Path(report_path).exists():
        return FileResponse(
            path=report_path,
            media_type="application/pdf",
            filename=Path(report_path).name,
        )

    # Fall back to globbing by trace_id
    trace_id = record.get("trace_id") or ""
    if trace_id:
        prefix  = f"incident_{trace_id[:8]}_"
        matches = sorted(_REPORTS_DIR.glob(f"{prefix}*.pdf"), reverse=True)
        if matches:
            return FileResponse(
                path=str(matches[0]),
                media_type="application/pdf",
                filename=matches[0].name,
            )

    raise HTTPException(status_code=404, detail="PDF report not yet generated")
