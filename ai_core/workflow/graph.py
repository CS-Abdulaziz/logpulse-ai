from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Bootstrap: add ai_core/cache/ and ai_core/workflow/agents/ to sys.path so
# cache nodes and agent modules can be imported without installed packages.
# ---------------------------------------------------------------------------
_WORKFLOW_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.normpath(
    os.path.join(_WORKFLOW_DIR, "..", "cache")
)
_AGENTS_DIR = os.path.normpath(
    os.path.join(_WORKFLOW_DIR, "agents")
)
_EVENTS_DIR = os.path.normpath(
    os.path.join(_WORKFLOW_DIR, "..", "events")
)
for _p in (_CACHE_DIR, _AGENTS_DIR, _EVENTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.classifier_agent import classifier_node
from agents.rag_agent import rag_node
from agents.history_agent import (
    history_node,
    configure_history,
    SqliteHistoryBackend,
)
from agents.diagnostic_agent import diagnostic_agent_node
from agents.solution_agent import solution_agent_node
from agents.risk_assessor import risk_assessor_node
from agents.human_in_the_loop import human_in_the_loop_node
from cache_node import cache_check_node, cache_write_node, route_after_cache
from recorder import record, build_incident_timeline
from models import EventType

from langgraph.graph import END, StateGraph

from state import (
    ClassificationData,
    CommandStep,
    DiagnosticAnalysis,
    DiagnosticResult,
    HistoryContext,
    HistoryResult,
    LogState,
    MergedKnowledge,
    RagResult,
    RoutingContext,
    SeverityLevel,
    SolutionAnalysis,
    SolutionResult,
    WorkflowStatus,
)

# ---------------------------------------------------------------------------
# Configure the SQLite history backend at application startup.
# Paths are resolved relative to the project root (two levels above this file).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.normpath(os.path.join(_WORKFLOW_DIR, "..", ".."))
_HISTORY_DB   = os.path.join(_PROJECT_ROOT, "data", "incident_history.sqlite3")

try:
    configure_history(SqliteHistoryBackend(_HISTORY_DB))
except Exception as _hist_err:
    print(f"[History] WARNING: could not open SQLite history DB: {_hist_err}")


def orchestrator_node(state: LogState) -> Dict[str, Any]:
    severity = (
        state.classification.severity
        if state.classification
        else SeverityLevel.INFO
    )

    if severity in {
        SeverityLevel.CRITICAL,
        SeverityLevel.FATAL,
    }:
        routing = RoutingContext(
            route="fast_track",
            priority="P1"
        )
    else:
        routing = RoutingContext(
            route="standard_flow",
            priority="P3"
        )

    return {"routing": routing}

def route_after_human(state: LogState) -> List[str]:
    if state.user_approved:
        return ["rag_node", "history_node"]
    return [END]


def merge_context_node(state: LogState) -> Dict[str, Any]:
    playbook = state.rag_result.playbook_steps if state.rag_result else None
    confidence = state.rag_result.rag_confidence if state.rag_result else None
    history = state.history_result.historical_incidents if state.history_result else None

    return {
        "merged_knowledge": MergedKnowledge(
            playbook_steps=playbook,
            rag_confidence=confidence,
            historical_incidents=history
        )
    }




def workflow_start_node(state: LogState) -> Dict[str, Any]:
    state.execution.workflow_status = WorkflowStatus.RUNNING
    record(
        incident_id=state.trace_id,
        event_type=EventType.INCIDENT_RECEIVED,
        node_name="workflow_start_node",
        message=f"Incident received (trace_id={state.trace_id[:8]}…)",
    )
    return {"execution": state.execution}

def workflow_complete_node(state: LogState) -> Dict[str, Any]:
    started_at = state.execution.started_at
    duration_ms = int((time.time() - started_at.timestamp()) * 1000)

    state.execution.workflow_status = WorkflowStatus.COMPLETED
    state.execution.processing_time_ms = duration_ms
    state.execution.completed_at = datetime.now(timezone.utc)

    record(
        incident_id=state.trace_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        node_name="workflow_complete_node",
        message=f"Completed in {duration_ms}ms",
        metadata={"duration_ms": duration_ms},
    )

    timeline = build_incident_timeline(state.trace_id)
    print(
        "\n"
        "\u2550" * 38 + "\n"
        "\U0001f4dc INCIDENT TIMELINE\n"
        "\u2550" * 38 + "\n"
        + timeline + "\n"
    )

    from ai_core.reports.pdf_generator import generate_report
    report_path = generate_report(state)
    if report_path:
        print(f"\n\U0001f4c4 Report saved: {report_path}")

    return {"execution": state.execution}

def build_workflow() -> StateGraph:
    workflow = StateGraph(LogState)

    workflow.add_node("workflow_start_node", workflow_start_node)
    workflow.add_node("cache_check_node", cache_check_node)
    workflow.add_node("classifier_node", classifier_node)
    workflow.add_node("cache_write_node", cache_write_node)
    workflow.add_node("orchestrator_node", orchestrator_node)
    workflow.add_node("human_in_the_loop_node", human_in_the_loop_node)
    workflow.add_node("rag_node", rag_node)
    workflow.add_node("history_node", history_node)
    workflow.add_node("merge_context_node", merge_context_node)
    workflow.add_node("diagnostic_agent_node", diagnostic_agent_node)
    workflow.add_node("solution_agent_node", solution_agent_node)
    workflow.add_node("risk_assessor_node", risk_assessor_node)
    workflow.add_node("workflow_complete_node", workflow_complete_node)

    workflow.set_entry_point("workflow_start_node")

    # --- Cache layer ---
    # workflow_start → cache_check
    workflow.add_edge("workflow_start_node", "cache_check_node")
    # cache_check → classifier (miss) or orchestrator (hit, skip LLM)
    workflow.add_conditional_edges(
        "cache_check_node",
        route_after_cache,
        {
            "cache_miss": "classifier_node",
            "cache_hit": "orchestrator_node",
        },
    )
    # After a fresh classification, write it to the cache then continue
    workflow.add_edge("classifier_node", "cache_write_node")
    workflow.add_edge("cache_write_node", "orchestrator_node")
    # --- End cache layer ---

    workflow.add_edge("orchestrator_node", "human_in_the_loop_node")

    workflow.add_conditional_edges(
        "human_in_the_loop_node",
        route_after_human
    )

    workflow.add_edge("rag_node", "merge_context_node")
    workflow.add_edge("history_node", "merge_context_node")

    workflow.add_edge("merge_context_node", "diagnostic_agent_node")
    workflow.add_edge("diagnostic_agent_node", "solution_agent_node")
    workflow.add_edge("solution_agent_node", "risk_assessor_node")
    workflow.add_edge("risk_assessor_node", "workflow_complete_node")
    workflow.add_edge("workflow_complete_node", END)

    return workflow.compile()

logpulse_app = build_workflow()