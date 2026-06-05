"""
tests/test_risk_assessor.py
============================
Unit tests for the standalone risk_assessor agent.

No external network calls.  No Gemini API key required.

Tests
-----
1. SAFE    : ["kubectl get pods", "free -m"]  → SAFE, 0 flagged
2. WARNING : ["systemctl restart postgresql"]  → WARNING, 1 flagged
3. FATAL   : ["rm -rf /var/lib/postgresql"]    → FATAL, 1 flagged
4. Mixed   : ["kubectl get pods", "rm -rf /"]  → FATAL (worst-case wins)
5. Empty   : []                                → SAFE, 0 flagged, 0 safe
6. matched_patterns correctly populated on FATAL hit
7. safe_commands and flagged_commands split correctly on a mixed list

All 7 must pass.  Must NOT break the existing 60 tests.

Run from project root:
    pytest tests/test_risk_assessor.py -v
    pytest tests/ -v
"""

from __future__ import annotations

import os
import sys

from ai_core.workflow.agents.risk_assessor import assess_commands, risk_assessor_node
from ai_core.workflow.state import LogState, RiskLevel, SolutionResult


# ===========================================================================
# 1. All-safe commands → SAFE
# ===========================================================================

def test_safe_commands_returns_safe():
    """
    Commands that match neither FATAL nor WARNING patterns must produce
    RiskLevel.SAFE with zero flagged commands and all in safe_commands.
    """
    cmds = ["kubectl get pods", "free -m"]
    result = assess_commands(cmds)

    assert result.final_risk_level == RiskLevel.SAFE
    assert result.flagged_commands == []
    assert set(result.safe_commands) == set(cmds)
    assert result.requires_approval_count == 0
    assert result.auto_executable_count == len(cmds)
    assert "SAFE" in result.message


# ===========================================================================
# 2. WARNING command (no FATAL) → WARNING
# ===========================================================================

def test_warning_command_returns_warning():
    """
    A command matching a WARNING pattern (and no FATAL pattern) must
    produce RiskLevel.WARNING and appear in flagged_commands.
    """
    cmds = ["systemctl restart postgresql"]
    result = assess_commands(cmds)

    assert result.final_risk_level == RiskLevel.WARNING
    assert len(result.flagged_commands) == 1
    assert result.flagged_commands[0] == cmds[0]
    assert result.safe_commands == []
    assert result.requires_approval_count == 1
    assert result.auto_executable_count == 0
    assert "WARNING" in result.message


# ===========================================================================
# 3. FATAL command → FATAL
# ===========================================================================

def test_fatal_command_returns_fatal():
    """
    A command matching a FATAL pattern must produce RiskLevel.FATAL and
    appear in flagged_commands.
    """
    cmds = ["rm -rf /var/lib/postgresql"]
    result = assess_commands(cmds)

    assert result.final_risk_level == RiskLevel.FATAL
    assert len(result.flagged_commands) == 1
    assert result.flagged_commands[0] == cmds[0]
    assert result.safe_commands == []
    assert result.requires_approval_count == 1
    assert result.auto_executable_count == 0
    assert "FATAL" in result.message


# ===========================================================================
# 4. Mixed list with one FATAL → overall result is FATAL
# ===========================================================================

def test_mixed_list_fatal_wins():
    """
    When a list contains both a safe command and a FATAL command, the
    overall risk level must be FATAL — worst-case precedence.
    """
    safe_cmd  = "kubectl get pods"
    fatal_cmd = "rm -rf /"
    result = assess_commands([safe_cmd, fatal_cmd])

    assert result.final_risk_level == RiskLevel.FATAL
    assert fatal_cmd in result.flagged_commands
    assert safe_cmd in result.safe_commands
    assert result.requires_approval_count == 1
    assert result.auto_executable_count == 1


# ===========================================================================
# 5. Empty command list → SAFE
# ===========================================================================

def test_empty_commands_returns_safe():
    """
    An empty command list must produce RiskLevel.SAFE with all counts
    at zero — there is nothing to flag.
    """
    result = assess_commands([])

    assert result.final_risk_level == RiskLevel.SAFE
    assert result.flagged_commands == []
    assert result.safe_commands == []
    assert result.matched_patterns == []
    assert result.requires_approval_count == 0
    assert result.auto_executable_count == 0


# ===========================================================================
# 6. matched_patterns populated correctly on FATAL
# ===========================================================================

def test_matched_patterns_populated_on_fatal():
    """
    matched_patterns must contain the exact raw regex string(s) that
    triggered the match so operators can audit which rule fired.
    """
    result = assess_commands(["rm -rf /data"])

    assert len(result.matched_patterns) >= 1
    # The FATAL rule for rm -rf is the first entry in FATAL_PATTERNS
    assert any("rm" in p for p in result.matched_patterns), (
        f"Expected an 'rm' pattern in matched_patterns, got: {result.matched_patterns}"
    )


# ===========================================================================
# 7. safe_commands and flagged_commands split correctly
# ===========================================================================

def test_safe_and_flagged_commands_split_correctly():
    """
    Given a 3-command list where exactly one triggers a WARNING pattern,
    the split between safe_commands and flagged_commands must be exact.
    """
    safe1  = "kubectl get pods -n default"
    warn   = "kubectl delete pod stale-worker-7f4d"
    safe2  = "df -h"

    result = assess_commands([safe1, warn, safe2])

    assert result.final_risk_level == RiskLevel.WARNING
    assert warn in result.flagged_commands
    assert safe1 in result.safe_commands
    assert safe2 in result.safe_commands
    assert len(result.flagged_commands) == 1
    assert len(result.safe_commands) == 2
    assert result.requires_approval_count == 1
    assert result.auto_executable_count == 2


# ===========================================================================
# Bonus: node function integration — correct dict key returned
# ===========================================================================

def test_risk_assessor_node_returns_security_check():
    """
    risk_assessor_node must return a dict with key 'security_check'
    whose value is a RiskAssessment.  Verify with a known-FATAL command.
    """
    from ai_core.workflow.state import RiskAssessment

    state = LogState(
        raw_log="[2026-05-28] FATAL: connection pool exhausted on db-primary",
        solution_result=SolutionResult(
            steps=["Increase pool size"],
            commands=["rm -rf /var/log/postgresql"],
            explanation="test",
            source="llm",
        ),
    )

    result = risk_assessor_node(state)

    assert "security_check" in result
    sc = result["security_check"]
    assert isinstance(sc, RiskAssessment)
    assert sc.final_risk_level == RiskLevel.FATAL
