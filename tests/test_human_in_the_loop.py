"""
tests/test_human_in_the_loop.py
================================
Unit tests for the human_in_the_loop_node and sandbox_executor.

All tests set LOGPULSE_AUTO_APPROVE=true to bypass input() and Docker.

Tests
-----
1. auto-approve → decision = APPROVED
2. reject → decision = REJECTED, sandbox not called
3. invalid then valid input → loops correctly (mock input())
4. sandbox unavailable → pipeline does not crash
5. sandbox timeout → stderr contains "[timeout after 15s]"
6. record_incident() called with correct outcome
7. OPERATOR_APPROVED event emitted on approval
8. OPERATOR_REJECTED event emitted on rejection

MUST NOT break existing 68 tests.

Run:
    pytest tests/test_human_in_the_loop.py -v
    pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from ai_core.events.event_bus import get_events_for_incident, reset_events
from ai_core.events.models import EventType
from ai_core.workflow.state import (
    ClassificationData,
    DiagnosticResult,
    HumanReviewResult,
    HistoryContext,
    LogState,
    OperatorDecision,
    RiskAssessment,
    RiskLevel,
    SeverityLevel,
    SolutionResult,
)
from ai_core.workflow.agents.human_in_the_loop import human_in_the_loop_node
from ai_core.workflow.agents.sandbox_executor import execute_in_sandbox


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_bus():
    reset_events()
    yield
    reset_events()


@pytest.fixture(autouse=True)
def _auto_approve_on(monkeypatch):
    """Default: LOGPULSE_AUTO_APPROVE=true for all tests in this file."""
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "true")


def _make_state(*, commands=None) -> LogState:
    """Build a minimal but complete LogState suitable for HITL testing."""
    return LogState(
        raw_log="[2026-05-28] OOMKilled: worker-pod-abc123 in namespace production",
        classification=ClassificationData(
            category="Memory",
            source="Qwen",
            severity=SeverityLevel.CRITICAL,
            summary="Container OOM killed",
        ),
        diagnostic_result=DiagnosticResult(
            root_cause="Memory leak in worker deployment",
            confidence=0.92,
            reasoning="OOMKilled event detected",
            used_history=False,
            used_playbook=True,
            source="llm",
        ),
        solution_result=SolutionResult(
            steps=["Restart deployment", "Check memory limits"],
            commands=commands or ["kubectl get pods -n production",
                                  "kubectl rollout restart deployment/worker"],
            explanation="Restart to clear the leak",
            source="llm",
        ),
        security_check=RiskAssessment(
            final_risk_level=RiskLevel.WARNING,
            flagged_commands=["kubectl rollout restart deployment/worker"],
            safe_commands=["kubectl get pods -n production"],
            message="WARNING",
            requires_approval_count=1,
            auto_executable_count=1,
        ),
        history_context=HistoryContext(
            is_first_occurrence=False,
            occurrence_count=3,
        ),
    )


# ===========================================================================
# 1. Auto-approve → decision = APPROVED
# ===========================================================================

def test_auto_approve_returns_approved():
    """
    With LOGPULSE_AUTO_APPROVE=true, human_in_the_loop_node must return
    user_approved=True and human_review.decision=APPROVED.
    """
    state = _make_state()
    # Prevent actual history write from crashing if SQLite is not configured
    with patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"):
        result = human_in_the_loop_node(state)

    assert result["user_approved"] is True
    review: HumanReviewResult = result["human_review"]
    assert review.decision == OperatorDecision.APPROVED


# ===========================================================================
# 2. Reject → decision = REJECTED, engine not called
# ===========================================================================

def test_reject_skips_sandbox(monkeypatch):
    """
    When the operator chooses 'r', user_approved must be False and
    no execution engine must be invoked.
    """
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "false")

    with (
        patch("builtins.input", return_value="r"),
        patch("ai_core.workflow.agents.human_in_the_loop.ExecutionEngine") as mock_engine,
        patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"),
    ):
        state = _make_state()
        result = human_in_the_loop_node(state)

    assert result["user_approved"] is False
    review: HumanReviewResult = result["human_review"]
    assert review.decision == OperatorDecision.REJECTED
    assert review.sandbox_results == []
    mock_engine.assert_not_called()


# ===========================================================================
# 3. Invalid then valid input → loops correctly
# ===========================================================================

def test_invalid_input_loops_until_valid(monkeypatch, capsys):
    """
    Garbage → garbage → 'a' — the node must re-prompt twice and then approve.
    """
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "false")
    inputs = iter(["xyz", "123", "a"])

    with (
        patch("builtins.input", side_effect=lambda _="": next(inputs)),
        patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"),
    ):
        state = _make_state()
        result = human_in_the_loop_node(state)

    assert result["user_approved"] is True
    captured = capsys.readouterr()
    assert captured.out.count("Invalid input") == 2


# ===========================================================================
# 4. Approval with no cluster state → ExecutionEngine uses default state
# ===========================================================================

def test_approval_without_cluster_state_does_not_crash():
    """
    When no cluster_state is provided (e.g. --log mode), the HITL node must
    fall back to seed_default_state() and execute commands via ExecutionEngine.
    No exit_code -1 may be returned for known kubectl commands.
    """
    os.environ["LOGPULSE_AUTO_APPROVE"] = "false"
    try:
        state = _make_state()
        assert state.cluster_state is None  # no state seeded by _make_state

        with (
            patch("builtins.input", return_value="a"),
            patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"),
        ):
            result = human_in_the_loop_node(state)

        assert result is not None
        sandbox_results = result["human_review"].sandbox_results
        # ExecutionEngine never returns exit_code -1 for known kubectl intents
        assert len(sandbox_results) > 0
        assert all(r.exit_code != -1 for r in sandbox_results), (
            f"Unexpected exit_code -1 in: {[r.exit_code for r in sandbox_results]}"
        )
    finally:
        os.environ["LOGPULSE_AUTO_APPROVE"] = "true"  # restore


# ===========================================================================
# 5. Sandbox timeout → stderr contains "[timeout after 15s]"
# ===========================================================================

def test_sandbox_timeout(monkeypatch):
    """
    When subprocess.run raises TimeoutExpired, stderr must contain
    the '[timeout after 15s]' marker and exit_code must be -1.
    """
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "false")

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=15),
    ):
        result = execute_in_sandbox("sleep 100", incident_id="")

    assert result.exit_code == -1
    assert "[timeout after 15s]" in result.stderr


# ===========================================================================
# 6. record_incident() called with correct outcome
# ===========================================================================

def test_record_incident_called_with_correct_outcome(monkeypatch):
    """
    _persist_incident() must be called exactly once and the history manager's
    record_incident() must receive outcome='approved_executed' on approval.
    """
    mock_mgr = MagicMock()
    with (
        patch("ai_core.workflow.agents.human_in_the_loop.get_history_manager", return_value=mock_mgr),
    ):
        state = _make_state()
        human_in_the_loop_node(state)

    mock_mgr.record_incident.assert_called_once()
    kwargs = mock_mgr.record_incident.call_args
    # outcome is a positional or keyword arg
    outcome = kwargs.kwargs.get("outcome") or kwargs.args[-1]
    assert outcome == "approved_executed"


def test_record_incident_rejected_outcome(monkeypatch):
    """On rejection, outcome passed to record_incident() must be 'rejected'."""
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "false")
    mock_mgr = MagicMock()

    with (
        patch("builtins.input", return_value="r"),
        patch("ai_core.workflow.agents.human_in_the_loop.get_history_manager", return_value=mock_mgr),
    ):
        state = _make_state()
        human_in_the_loop_node(state)

    mock_mgr.record_incident.assert_called_once()
    kwargs = mock_mgr.record_incident.call_args
    outcome = kwargs.kwargs.get("outcome") or kwargs.args[-1]
    assert outcome == "rejected"


# ===========================================================================
# 7. OPERATOR_APPROVED event emitted on approval
# ===========================================================================

def test_operator_approved_event_emitted():
    """
    After approval, the event bus must contain an OPERATOR_APPROVED event
    for the correct incident_id.
    """
    with patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"):
        state = _make_state()
        human_in_the_loop_node(state)

    events = get_events_for_incident(state.trace_id)
    event_types = [e.event_type for e in events]
    assert EventType.OPERATOR_APPROVED in event_types


# ===========================================================================
# 8. OPERATOR_REJECTED event emitted on rejection
# ===========================================================================

def test_operator_rejected_event_emitted(monkeypatch):
    """
    After rejection, the event bus must contain an OPERATOR_REJECTED event.
    """
    monkeypatch.setenv("LOGPULSE_AUTO_APPROVE", "false")

    with (
        patch("builtins.input", return_value="r"),
        patch("ai_core.workflow.agents.human_in_the_loop._persist_incident"),
    ):
        state = _make_state()
        human_in_the_loop_node(state)

    events = get_events_for_incident(state.trace_id)
    event_types = [e.event_type for e in events]
    assert EventType.OPERATOR_REJECTED in event_types
