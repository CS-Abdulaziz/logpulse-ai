"""
human_in_the_loop.py — Operator review gate for the LogPulse pipeline.

Console layout
--------------
╔══════════════════════════════════════════════════╗
║           LOGPULSE — OPERATOR REVIEW             ║
╚══════════════════════════════════════════════════╝

INCIDENT
  Category  : Memory
  Severity  : Critical
  Seen      : 3 times before

ROOT CAUSE
  Memory leak in worker pod — container OOM killed

PROPOSED COMMANDS
  [SAFE]    kubectl get pods -n production
  [WARNING] kubectl rollout restart deployment/worker

RISK ASSESSMENT
  Level     : WARNING
  Flagged   : 1 commands require attention

──────────────────────────────────────────────────
  [a] Approve & execute in Docker sandbox
  [r] Reject
──────────────────────────────────────────────────
Decision:

Auto-approve
------------
Set LOGPULSE_AUTO_APPROVE=true to bypass input() and auto-approve.
Required for test suites and CI pipelines.

Events emitted
--------------
OPERATOR_APPROVED or OPERATOR_REJECTED — immediately after the decision.
SANDBOX_EXECUTED / SANDBOX_FAILED — via sandbox_executor, one per command.
INCIDENT_RECORDED — after history is updated.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_AGENTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_EVENTS_DIR   = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "events"))
_CACHE_DIR    = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "cache"))

for _p in (_WORKFLOW_DIR, _EVENTS_DIR, _CACHE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import (                          # noqa: E402
    HumanReviewResult,
    LogState,
    OperatorDecision,
    RiskLevel,
    SandboxResult,
)
from recorder import record                  # noqa: E402
from models import EventType                 # noqa: E402
from sandbox_executor import execute_commands_in_sandbox  # noqa: E402
from history_agent import get_history_manager             # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_AUTO_APPROVE_ENV = "LOGPULSE_AUTO_APPROVE"
_BORDER = "─" * 50


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _label_command(cmd: str, security_check) -> str:
    """Return [FATAL], [WARNING], or [SAFE] prefix for a command."""
    if security_check is None:
        return "[SAFE]   "
    if cmd in (security_check.flagged_commands or []):
        level = security_check.final_risk_level
        if level == RiskLevel.FATAL:
            return "[FATAL]  "
        return "[WARNING]"
    return "[SAFE]   "


def _print_review_panel(state: LogState) -> None:
    """Print the full operator review panel to stdout."""
    cls = state.classification
    diag = state.diagnostic_result
    sol = state.solution_result
    sc = state.security_check
    hist = state.history_context

    category = cls.category if cls else "Unknown"
    severity = cls.severity.value if cls else "Unknown"
    seen_count = (hist.occurrence_count if hist and not hist.is_first_occurrence else 0)
    root_cause = diag.root_cause if diag else "(not available)"

    commands: List[str] = []
    if sol:
        commands = sol.commands or []

    risk_level = sc.final_risk_level.value if sc else "unknown"
    flagged_count = len(sc.flagged_commands) if sc else 0

    print()
    print("╔" + "═" * 50 + "╗")
    print("║" + "           LOGPULSE — OPERATOR REVIEW             " + "║")
    print("╚" + "═" * 50 + "╝")
    print()
    print("INCIDENT")
    print(f"  Category  : {category}")
    print(f"  Severity  : {severity}")
    if seen_count:
        print(f"  Seen      : {seen_count} times before")
    else:
        print("  Seen      : First occurrence")
    print()
    print("ROOT CAUSE")
    for line in (root_cause or "").split("\n"):
        print(f"  {line}")
    print()
    print("PROPOSED COMMANDS")
    if commands:
        for cmd in commands:
            lbl = _label_command(cmd, sc)
            print(f"  {lbl}  {cmd}")
    else:
        print("  (no commands proposed)")
    print()
    print("RISK ASSESSMENT")
    print(f"  Level     : {risk_level}")
    print(f"  Flagged   : {flagged_count} commands require attention")
    print()
    print(_BORDER)
    print("  [a] Approve & execute in Docker sandbox")
    print("  [r] Reject")
    print(_BORDER)


def _get_decision(auto_approve: bool) -> str:
    """
    Return 'a' (approve) or 'r' (reject).
    Loops on invalid input until a valid answer is given.
    """
    if auto_approve:
        print("Decision: a  [auto-approved via LOGPULSE_AUTO_APPROVE]")
        return "a"

    while True:
        raw = input("Decision: ").strip().lower()
        if raw in ("a", "approve"):
            return "a"
        if raw in ("r", "reject"):
            return "r"
        print("  Invalid input — please enter 'a' to approve or 'r' to reject.")


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def human_in_the_loop_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — operator review gate.

    Reads:
        state.classification, diagnostic_result, solution_result,
        security_check, history_context

    Writes:
        state.user_approved  (bool)
        state.human_review   (HumanReviewResult)

    Behaviour
    ---------
    1. Print the review panel.
    2. Prompt for 'a' / 'r' (loops on invalid input).
    3. Emit OPERATOR_APPROVED or OPERATOR_REJECTED.
    4. On approval → execute commands in Docker sandbox.
    5. Call record_incident() to persist outcome in history.

    LOGPULSE_AUTO_APPROVE=true skips input() and Docker execution.
    """
    auto_approve = os.environ.get(_AUTO_APPROVE_ENV, "").lower() == "true"

    _print_review_panel(state)

    decision_char = _get_decision(auto_approve)
    approved = decision_char == "a"
    decision = OperatorDecision.APPROVED if approved else OperatorDecision.REJECTED

    # ── Emit operator decision event ─────────────────────────────────────────
    event_type = (
        EventType.OPERATOR_APPROVED if approved else EventType.OPERATOR_REJECTED
    )
    record(
        incident_id=state.trace_id,
        event_type=event_type,
        node_name="human_in_the_loop_node",
        message="approved" if approved else "rejected",
    )

    # ── Execute commands in sandbox (approval only) ───────────────────────────
    sandbox_results: List[SandboxResult] = []
    if approved:
        sol = state.solution_result
        commands = sol.commands if sol else []
        if commands and not auto_approve:
            print("\n[Sandbox] Executing commands...\n")
            sandbox_results = execute_commands_in_sandbox(
                commands,
                incident_id=state.trace_id,
                cluster_state=state.cluster_state,
            )
        elif commands and auto_approve:
            # Auto-approve: simulate execution without Docker
            for cmd in commands:
                sandbox_results.append(SandboxResult(
                    command=cmd,
                    stdout=f"[auto-approve] simulated: {cmd}",
                    stderr="",
                    exit_code=0,
                    execution_time_ms=0,
                ))

    # ── Persist in incident history ───────────────────────────────────────────
    outcome_str = "approved_executed" if approved else "rejected"
    _persist_incident(state, outcome_str)

    # ── Build HumanReviewResult ───────────────────────────────────────────────
    review = HumanReviewResult(
        decision=decision,
        operator_note="",
        sandbox_results=sandbox_results,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )

    return {
        "user_approved": approved,
        "human_review": review,
    }


# ---------------------------------------------------------------------------
# History persistence helper
# ---------------------------------------------------------------------------

def _persist_incident(state: LogState, outcome: str) -> None:
    """Write this incident to the history store after the decision is made."""
    try:
        mgr = get_history_manager()
        cls = state.classification
        diag = state.diagnostic_result
        sol = state.solution_result

        mgr.record_incident(
            raw_log=state.raw_log,
            category=cls.category if cls else "Unknown",
            source=cls.source if cls else "Unknown",
            severity=cls.severity.value if cls else "Info",
            summary=cls.summary if cls else "",
            root_cause=diag.root_cause if diag else None,
            applied_solution=(
                "; ".join(sol.steps) if sol and sol.steps else None
            ),
            outcome=outcome,
        )
        record(
            incident_id=state.trace_id,
            event_type=EventType.INCIDENT_RECORDED,
            node_name="human_in_the_loop_node",
            message=f"Incident recorded — outcome={outcome}",
            metadata={"outcome": outcome},
        )
    except Exception as exc:
        print(f"[HITL] WARNING: could not persist incident history: {exc}")
