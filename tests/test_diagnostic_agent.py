"""
tests/test_diagnostic_agent.py
================================
Unit tests for the LogPulse diagnostic_agent_node.

All tests mock the Gemini API — no real network calls are made and no
GEMINI_API_KEY is required for the suite to pass.

Tests
-----
1. test_llm_success_source_and_fields
       Gemini returns valid JSON → result has source="llm", parsed fields,
       and correct used_history / used_playbook flags.

2. test_gemini_fails_strong_playbook_uses_fallback
       Gemini raises; rag_result.confidence >= threshold →
       source="playbook_fallback", used_playbook=True.

3. test_gemini_fails_no_playbook_safe_fallback
       Gemini raises; rag_result is absent → source="safe_fallback",
       confidence=0.1, both used_* False.

4. test_recurring_incident_sets_used_history
       history_context.is_first_occurrence=False + Gemini success →
       used_history=True.

5. test_malformed_gemini_json_routes_to_fallback
       Gemini returns non-JSON garbage → ValueError caught → playbook
       fallback (if strong) or safe_fallback. No exception leaks out.

6. test_node_never_raises_on_unexpected_exception
       Any unexpected exception inside the node must still yield a valid
       DiagnosticResult dict — never re-raise out of the node.

7. test_confidence_clamped_to_valid_range
       LLM returns confidence > 1.0 or < 0.0 → clamped to [0.0, 1.0].

8. test_gemini_markdown_fence_variants
       _call_gemini correctly strips all real-world Markdown fence patterns
       emitted by the model (```json own-line, ``` own-line, inline variant)
       and still returns valid parsed JSON.

Run from project root:
    pytest tests/test_diagnostic_agent.py -v
"""

from __future__ import annotations

import os
import sys
from unittest import mock

# ---------------------------------------------------------------------------
# Path bootstrap — works from project root (pytest) and inside workflow/ dir
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
import diagnostic_agent                                          # noqa: E402
from diagnostic_agent import (                                   # noqa: E402
    diagnostic_agent_node,
    PLAYBOOK_FALLBACK_THRESHOLD,
    _build_prompt,
    _playbook_fallback,
    _safe_fallback,
    _call_gemini,
)
from state import (                                              # noqa: E402
    ClassificationData,
    DiagnosticResult,
    HistoryContext,
    LogState,
    RagResult,
    SeverityLevel,
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

_STRONG_RAG = RagResult(
    playbook_steps=(
        "[Playbook: Kubernetes OOMKilled — Memory Limit Exceeded]\n"
        "Severity: Critical\n\n"
        "Resolution steps:\n"
        "  • Check current memory requests/limits with kubectl describe pod\n"
        "  • Increase memory limit in the Deployment spec\n"
        "  • Consider adding a Vertical Pod Autoscaler"
    ),
    rag_confidence=0.72,   # >= PLAYBOOK_FALLBACK_THRESHOLD (0.5)
)

_WEAK_RAG = RagResult(
    playbook_steps="[RAG] No matching playbook found. Reason: best confidence 0.2 below threshold 0.35",
    rag_confidence=0.0,
)

_FIRST_HISTORY = HistoryContext(
    is_first_occurrence=True,
    occurrence_count=0,
)

_RECURRING_HISTORY = HistoryContext(
    is_first_occurrence=False,
    occurrence_count=3,
    previous_root_cause="Unbounded in-memory cache growth caused OOMKill.",
    previous_solution="Increased memory limit to 4Gi.",
    previous_outcome="simulated_success",
)

_GEMINI_JSON = {
    "root_cause": "The container's in-memory cache grew unbounded and exceeded the 2Gi Kubernetes memory limit, triggering an OOMKill event.",
    "confidence": 0.88,
    "reasoning": "OOMKilled log + Memory playbook + 3 prior occurrences with identical root cause confirmed the cache growth pattern.",
}


def _make_state(
    rag: RagResult | None = _STRONG_RAG,
    history: HistoryContext | None = _FIRST_HISTORY,
) -> LogState:
    """Return a fully populated LogState ready for diagnostic_agent_node."""
    return LogState(
        raw_log=_OOM_LOG,
        classification=_GOOD_CLASSIFICATION,
        rag_result=rag,
        history_context=history,
    )


def _mock_gemini_success(payload: dict = _GEMINI_JSON):
    """Patch _call_gemini to return *payload* without hitting the network."""
    return mock.patch(
        "diagnostic_agent._call_gemini",
        return_value=payload,
    )


def _mock_gemini_fail(exc=RuntimeError("Simulated Gemini API failure")):
    """Patch _call_gemini to raise *exc*."""
    return mock.patch(
        "diagnostic_agent._call_gemini",
        side_effect=exc,
    )


# ---------------------------------------------------------------------------
# Helper: force GEMINI_AVAILABLE=True regardless of local env
# ---------------------------------------------------------------------------
def _force_gemini_available(available: bool = True):
    """Context manager that sets the module-level GEMINI_AVAILABLE flag."""
    return mock.patch.object(diagnostic_agent, "GEMINI_AVAILABLE", available)


# ===========================================================================
# 1. LLM success — correct fields, source, and flags
# ===========================================================================

def test_llm_success_source_and_fields():
    """
    With Gemini mocked to return valid JSON, diagnostic_agent_node must:
    - return source="llm"
    - populate root_cause, confidence, reasoning from the parsed JSON
    - set used_playbook=True  (rag_confidence >= threshold)
    - set used_history=False  (first occurrence)
    """
    state = _make_state(rag=_STRONG_RAG, history=_FIRST_HISTORY)

    with _force_gemini_available(True), _mock_gemini_success():
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]

    assert isinstance(dr, DiagnosticResult)
    assert dr.source == "llm"
    assert dr.root_cause == _GEMINI_JSON["root_cause"]
    assert dr.confidence == 0.88
    assert dr.reasoning == _GEMINI_JSON["reasoning"]
    assert dr.used_playbook is True   # rag_confidence 0.72 >= 0.5
    assert dr.used_history is False   # first occurrence


# ===========================================================================
# 2. Gemini fails + strong playbook → playbook_fallback
# ===========================================================================

def test_gemini_fails_strong_playbook_uses_fallback():
    """
    When _call_gemini raises AND rag_confidence >= PLAYBOOK_FALLBACK_THRESHOLD,
    the node must return source="playbook_fallback" with used_playbook=True.
    """
    state = _make_state(rag=_STRONG_RAG, history=_FIRST_HISTORY)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]

    assert dr.source == "playbook_fallback"
    assert dr.used_playbook is True
    assert dr.used_history is False
    assert 0.0 < dr.confidence <= 0.6   # capped at 0.6 for non-LLM diagnosis
    assert "OOMKilled" in dr.root_cause or "Memory" in dr.root_cause or "Kubernetes" in dr.root_cause


# ===========================================================================
# 3. Gemini fails + no/weak playbook → safe_fallback
# ===========================================================================

def test_gemini_fails_no_playbook_safe_fallback():
    """
    When _call_gemini raises AND the rag result is absent/weak,
    the node must return source="safe_fallback", confidence=0.1,
    used_history=False, used_playbook=False.
    """
    state = _make_state(rag=_WEAK_RAG, history=_FIRST_HISTORY)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]

    assert dr.source == "safe_fallback"
    assert dr.confidence == 0.1
    assert dr.used_history is False
    assert dr.used_playbook is False
    assert "manual investigation" in dr.root_cause.lower()


# ===========================================================================
# 4. Recurring incident sets used_history=True
# ===========================================================================

def test_recurring_incident_sets_used_history():
    """
    When history_context.is_first_occurrence=False, and Gemini succeeds,
    the returned DiagnosticResult must have used_history=True.
    """
    state = _make_state(rag=_STRONG_RAG, history=_RECURRING_HISTORY)

    with _force_gemini_available(True), _mock_gemini_success():
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]

    assert dr.source == "llm"
    assert dr.used_history is True    # recurring, not first occurrence


# ===========================================================================
# 5. Malformed Gemini JSON → routes to fallback, doesn't crash
# ===========================================================================

def test_malformed_gemini_json_routes_to_fallback():
    """
    If Gemini returns garbage (no valid JSON), a ValueError is raised inside
    _call_gemini.  The node must catch it and route to playbook_fallback
    (since _STRONG_RAG is provided) without propagating any exception.
    """
    state = _make_state(rag=_STRONG_RAG, history=_FIRST_HISTORY)

    with _force_gemini_available(True), mock.patch(
        "diagnostic_agent._call_gemini",
        side_effect=ValueError("No JSON object found"),
    ):
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]

    # Should fall back to playbook (rag_confidence=0.72 >= threshold)
    assert dr.source == "playbook_fallback"
    assert isinstance(dr, DiagnosticResult)


# ===========================================================================
# 6. Node never raises on unexpected exception
# ===========================================================================

def test_node_never_raises_on_unexpected_exception():
    """
    Any unexpected exception inside diagnostic_agent_node must NOT propagate
    out of the node.  Even with no rag_result and a broken Gemini call, the
    node must return a valid dict with a DiagnosticResult.
    """
    state = _make_state(rag=None, history=None)

    with _force_gemini_available(True), mock.patch(
        "diagnostic_agent._call_gemini",
        side_effect=Exception("Totally unexpected failure"),
    ):
        # Must not raise
        result = diagnostic_agent_node(state)

    assert "diagnostic_result" in result
    dr: DiagnosticResult = result["diagnostic_result"]
    assert isinstance(dr, DiagnosticResult)
    assert dr.source == "safe_fallback"


# ===========================================================================
# 7. Confidence clamped to [0.0, 1.0]
# ===========================================================================

def test_confidence_clamped_to_valid_range():
    """
    If the LLM returns confidence values outside [0.0, 1.0] (e.g. 1.5 or -0.2),
    the node must clamp them before creating DiagnosticResult.
    """
    state = _make_state(rag=_STRONG_RAG, history=_FIRST_HISTORY)

    # Test: LLM returns confidence > 1.0
    over_payload = {**_GEMINI_JSON, "confidence": 1.95}
    with _force_gemini_available(True), _mock_gemini_success(over_payload):
        result = diagnostic_agent_node(state)
    assert result["diagnostic_result"].confidence == 1.0

    # Test: LLM returns confidence < 0.0
    under_payload = {**_GEMINI_JSON, "confidence": -0.5}
    with _force_gemini_available(True), _mock_gemini_success(under_payload):
        result = diagnostic_agent_node(state)
    assert result["diagnostic_result"].confidence == 0.0


# ===========================================================================
# 8. Markdown fence variants — Bug 1 regression guard
# ===========================================================================

def test_gemini_markdown_fence_variants():
    """
    _call_gemini must successfully parse JSON when Gemini wraps its response
    in Markdown code fences.  Three production-observed variants are tested:

    a) Fences on their own lines (the most common pattern):
           ```json
           { ... }
           ```

    b) Plain triple-backtick fence (no language tag):
           ```
           { ... }
           ```

    c) Inline variant (no newline between fence and content):
           ```json{...}```

    All three must produce the same parsed dict without routing to any
    fallback path and without raising any exception.
    """
    import json as _json
    from unittest.mock import MagicMock

    _raw = {
        "root_cause": "Fenced JSON parsed correctly.",
        "confidence": 0.77,
        "reasoning": "Fence stripping worked.",
    }
    _raw_str = _json.dumps(_raw)

    fence_variants = [
        # a) ```json own-line opener + own-line closer
        f"```json\n{_raw_str}\n```",
        # b) ``` own-line opener (no language tag)
        f"```\n{_raw_str}\n```",
        # c) inline — no newline between fence and payload
        f"```json{_raw_str}```",
    ]

    for variant in fence_variants:
        # Build a mock response object that has a .text attribute
        mock_response = MagicMock()
        mock_response.text = variant

        # Replace _genai_client wholesale with a MagicMock so we never touch
        # the real google.genai.Client object — its `.models` attribute is a
        # read-only property and cannot be patched via patch.object.
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            _force_gemini_available(True),
            mock.patch("diagnostic_agent._genai_client", mock_client),
        ):
            parsed = _call_gemini("dummy prompt")

        assert parsed["root_cause"] == _raw["root_cause"], (
            f"Fence variant failed to parse correctly:\n{variant!r}"
        )
        assert parsed["confidence"] == _raw["confidence"]
        assert parsed["reasoning"] == _raw["reasoning"]
