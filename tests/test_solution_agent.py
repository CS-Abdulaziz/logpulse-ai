"""
tests/test_solution_agent.py
==============================
Unit tests for the LogPulse solution_agent_node.

All tests mock the Gemini API — no real network calls are made and no
GEMINI_API_KEY is required for the suite to pass.

Tests
-----
1. test_llm_success_source_and_fields
       Gemini returns valid JSON → result has source="llm", non-empty steps,
       non-empty commands, and a populated explanation.

2. test_gemini_fails_strong_playbook_uses_fallback
       Gemini raises; rag_confidence >= threshold → source="playbook_fallback",
       steps extracted from playbook bullets.

3. test_gemini_fails_no_playbook_safe_fallback
       Gemini raises; rag_result absent → source="safe_fallback",
       generic triage steps, empty commands.

4. test_malformed_json_routes_to_fallback
       Gemini returns non-JSON garbage → ValueError caught → playbook or
       safe fallback.  Node never propagates exception.

5. test_node_never_raises
       Any unexpected exception inside the node yields a valid SolutionResult
       dict — never re-raises.

6. test_llm_normalises_string_steps
       If the LLM returns steps/commands as a comma-separated string instead
       of a list, the node normalises them into List[str].

7. test_safe_fallback_has_no_commands
       safe_fallback always returns an empty commands list to require human
       judgment before execution.

Run from project root:
    pytest tests/test_solution_agent.py -v
"""

from __future__ import annotations

import os
import sys
from unittest import mock
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT      = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKFLOW  = os.path.join(_ROOT, "ai_core", "workflow")
_AGENTS    = os.path.join(_WORKFLOW, "agents")
_CACHE_DIR = os.path.join(_ROOT, "ai_core", "cache")

for _p in (_WORKFLOW, _AGENTS, _CACHE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import solution_agent                                            # noqa: E402
from solution_agent import (                                    # noqa: E402
    solution_agent_node,
    PLAYBOOK_FALLBACK_THRESHOLD,
    _build_prompt,
    _playbook_fallback,
    _safe_fallback,
    _call_gemini,
)
from state import (                                             # noqa: E402
    ClassificationData,
    DiagnosticResult,
    HistoryContext,
    LogState,
    RagResult,
    SeverityLevel,
    SolutionResult,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_OOM_LOG = (
    "2026-05-15T08:33:14Z pod/worker-deployment-5f7c "
    "OOMKilled: container 'worker' exceeded memory limit 2Gi"
)

_GOOD_CLASSIFICATION = ClassificationData(
    category="Memory",
    source="Kubernetes",
    severity=SeverityLevel.CRITICAL,
    summary="Container OOMKilled — exceeded 2Gi memory limit.",
)

_GOOD_DIAGNOSTIC = DiagnosticResult(
    root_cause=(
        "The container's in-memory cache grew unbounded and exceeded "
        "the 2Gi Kubernetes memory limit, triggering an OOMKill event."
    ),
    confidence=0.88,
    reasoning="OOMKilled log + Memory playbook.",
    used_history=False,
    used_playbook=True,
    source="llm",
)

_STRONG_RAG = RagResult(
    playbook_steps=(
        "[Playbook: Kubernetes OOMKilled — Memory Limit Exceeded]\n"
        "Severity: Critical\n\n"
        "Resolution steps:\n"
        "  • Check current memory requests/limits with kubectl describe pod\n"
        "  • Increase memory limit in the Deployment spec\n"
        "  • kubectl patch deployment worker-deployment "
        "-p '{\"spec\":{\"template\":{\"spec\":{\"containers\":"
        "[{\"name\":\"worker\",\"resources\":{\"limits\":{\"memory\":\"4Gi\"}}}]}}}}'  \n"
        "  • Consider adding a Vertical Pod Autoscaler"
    ),
    rag_confidence=0.72,   # >= PLAYBOOK_FALLBACK_THRESHOLD (0.5)
)

_WEAK_RAG = RagResult(
    playbook_steps="[RAG] No matching playbook found.",
    rag_confidence=0.0,
)

_GEMINI_JSON = {
    "steps": [
        "Increase the memory limit for the worker container to 4Gi.",
        "Rolling-restart the deployment to pick up the new limit.",
    ],
    "commands": [
        "kubectl set resources deployment/worker-deployment "
        "--limits=memory=4Gi -n default",
        "kubectl rollout restart deployment/worker-deployment -n default",
    ],
    "explanation": (
        "The OOMKill was caused by the container exceeding its 2Gi memory limit. "
        "Increasing the limit to 4Gi and restarting the deployment will resolve "
        "the immediate issue while a longer-term fix (memory leak analysis) is pursued."
    ),
}


def _make_state(
    rag: RagResult | None = _STRONG_RAG,
    diag: DiagnosticResult | None = _GOOD_DIAGNOSTIC,
) -> LogState:
    return LogState(
        raw_log=_OOM_LOG,
        classification=_GOOD_CLASSIFICATION,
        rag_result=rag,
        diagnostic_result=diag,
    )


def _mock_gemini_success(payload: dict = _GEMINI_JSON):
    return mock.patch("solution_agent._call_gemini", return_value=payload)


def _mock_gemini_fail(exc=RuntimeError("Simulated Gemini API failure")):
    return mock.patch("solution_agent._call_gemini", side_effect=exc)


def _force_gemini_available(available: bool = True):
    return mock.patch.object(solution_agent, "GEMINI_AVAILABLE", available)


# ===========================================================================
# 1. LLM success — fields, source, lists
# ===========================================================================

def test_llm_success_source_and_fields():
    """
    With Gemini mocked to return valid JSON, solution_agent_node must:
    - return source="llm"
    - populate steps, commands (both non-empty lists), explanation
    - not raise
    """
    state = _make_state()

    with _force_gemini_available(True), _mock_gemini_success():
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]

    assert isinstance(sr, SolutionResult)
    assert sr.source == "llm"
    assert isinstance(sr.steps, list) and len(sr.steps) >= 1
    assert isinstance(sr.commands, list) and len(sr.commands) >= 1
    assert isinstance(sr.explanation, str) and len(sr.explanation) > 0
    # Commands must not reference PostgreSQL for an OOMKilled incident
    assert not any("postgresql" in cmd.lower() for cmd in sr.commands), (
        "Commands should not contain PostgreSQL references for a K8s OOMKilled incident"
    )


# ===========================================================================
# 2. Gemini fails + strong playbook → playbook_fallback
# ===========================================================================

def test_gemini_fails_strong_playbook_uses_fallback():
    """
    When _call_gemini raises AND rag_confidence >= threshold:
    source="playbook_fallback", steps extracted from playbook bullets.
    """
    state = _make_state(rag=_STRONG_RAG)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]

    assert sr.source == "playbook_fallback"
    assert len(sr.steps) >= 1, "Playbook fallback must extract at least one step"
    assert isinstance(sr.explanation, str) and "Gemini unavailable" in sr.explanation


# ===========================================================================
# 3. Gemini fails + no playbook → safe_fallback
# ===========================================================================

def test_gemini_fails_no_playbook_safe_fallback():
    """
    When _call_gemini raises AND rag_result is absent or weak:
    source="safe_fallback", generic triage steps, empty commands.
    """
    state = _make_state(rag=_WEAK_RAG)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]

    assert sr.source == "safe_fallback"
    assert len(sr.steps) >= 1, "Safe fallback must emit at least one triage step"
    assert sr.commands == [], "Safe fallback must not emit commands (requires human judgment)"


# ===========================================================================
# 4. Malformed Gemini JSON → routes to fallback, doesn't crash
# ===========================================================================

def test_malformed_json_routes_to_fallback():
    """
    If Gemini returns garbage (no valid JSON), ValueError propagates to
    the except block → playbook_fallback (strong RAG present) without crash.
    """
    state = _make_state(rag=_STRONG_RAG)

    with _force_gemini_available(True), mock.patch(
        "solution_agent._call_gemini",
        side_effect=ValueError("No JSON object found"),
    ):
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]
    assert sr.source == "playbook_fallback"
    assert isinstance(sr, SolutionResult)


# ===========================================================================
# 5. Node never raises on unexpected exception
# ===========================================================================

def test_node_never_raises():
    """
    Any unexpected exception inside solution_agent_node must NOT propagate.
    Even with no rag_result and a broken Gemini call, we get a SolutionResult.
    """
    state = _make_state(rag=None, diag=None)

    with _force_gemini_available(True), mock.patch(
        "solution_agent._call_gemini",
        side_effect=Exception("Totally unexpected failure"),
    ):
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]
    assert isinstance(sr, SolutionResult)
    assert sr.source == "safe_fallback"


# ===========================================================================
# 6. LLM returns comma-string instead of list — normalised correctly
# ===========================================================================

def test_llm_normalises_string_steps():
    """
    If the LLM returns steps/commands as a plain comma-separated string
    instead of a JSON array, the node must normalise them into List[str].
    """
    string_payload = {
        "steps": "Increase memory limit, Rolling-restart deployment",
        "commands": (
            "kubectl set resources deployment/worker --limits=memory=4Gi, "
            "kubectl rollout restart deployment/worker"
        ),
        "explanation": "Memory limit increase resolves the OOMKill.",
    }
    state = _make_state()

    with _force_gemini_available(True), _mock_gemini_success(string_payload):
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert isinstance(sr.steps, list) and len(sr.steps) >= 1
    assert isinstance(sr.commands, list) and len(sr.commands) >= 1
    assert sr.source == "llm"


# ===========================================================================
# 7. Safe fallback always produces empty commands
# ===========================================================================

def test_safe_fallback_has_no_commands():
    """
    _safe_fallback must always return an empty commands list.
    Automated commands without human review are too risky for a safe fallback.
    """
    sf = _safe_fallback("test reason")
    assert sf.source == "safe_fallback"
    assert sf.commands == [], "safe_fallback must not produce any commands"
    assert len(sf.steps) >= 1, "safe_fallback must produce at least one triage step"
