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

from ai_core.workflow.agents import solution_agent
from ai_core.workflow.agents.solution_agent import (
    solution_agent_node,
    PLAYBOOK_FALLBACK_THRESHOLD,
    _build_prompt,
    _parse_steps_from_playbook,
    _playbook_fallback,
    _safe_fallback,
    _call_gemini,
    _parse_llm_response,
)
from ai_core.workflow.state import (
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
    "root_cause": (
        "The OOMKill was caused by the container exceeding its 2Gi memory limit. "
        "Increasing the limit to 4Gi and restarting the deployment will resolve "
        "the immediate issue while a longer-term fix (memory leak analysis) is pursued."
    ),
    "commands": [
        {
            "cmd": "kubectl set resources deployment/worker-deployment "
                   "--limits=memory=4Gi -n default",
            "risk": "warning",
            "description": "Increase the memory limit for the worker container to 4Gi.",
        },
        {
            "cmd": "kubectl rollout restart deployment/worker-deployment -n default",
            "risk": "warning",
            "description": "Rolling-restart the deployment to pick up the new limit.",
        },
    ],
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
    return mock.patch("ai_core.workflow.agents.solution_agent._call_gemini", return_value=payload)


def _mock_gemini_fail(exc=RuntimeError("Simulated Gemini API failure")):
    return mock.patch("ai_core.workflow.agents.solution_agent._call_gemini", side_effect=exc)


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
        "ai_core.workflow.agents.solution_agent._call_gemini",
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
        "ai_core.workflow.agents.solution_agent._call_gemini",
        side_effect=Exception("Totally unexpected failure"),
    ):
        result = solution_agent_node(state)

    assert "solution_result" in result
    sr: SolutionResult = result["solution_result"]
    assert isinstance(sr, SolutionResult)
    assert sr.source == "safe_fallback"


# ===========================================================================
# 6. LLM response robustness — new structured format edge cases
# ===========================================================================

def test_llm_filters_empty_cmd_fields():
    """
    In the new structured format, commands with empty or missing 'cmd'
    fields must be silently filtered — only entries with a non-empty 'cmd'
    appear in solution_result.commands.
    """
    partial_payload = {
        "root_cause": "Memory limit exceeded.",
        "commands": [
            {
                "cmd": "kubectl rollout restart deployment/worker-deployment -n default",
                "risk": "warning",
                "description": "Rolling-restart to apply new limits.",
            },
            {
                "cmd": "",                # empty cmd → must be dropped
                "risk": "safe",
                "description": "Step with no command.",
            },
            {
                "description": "Step with no cmd key at all.",  # no 'cmd' key → dropped
                "risk": "safe",
            },
        ],
    }
    state = _make_state()

    with _force_gemini_available(True), _mock_gemini_success(partial_payload):
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert isinstance(sr.steps, list)
    assert isinstance(sr.commands, list)
    # Only the one entry with a real 'cmd' should survive
    assert len(sr.commands) == 1
    assert "kubectl rollout restart" in sr.commands[0]
    assert sr.source == "llm"


def test_llm_legacy_flat_format_still_works():
    """
    The legacy flat format {steps, commands, explanation} must still be
    accepted for backward compatibility (e.g. older model outputs).
    """
    legacy_payload = {
        "steps": [
            "Increase memory limit for the worker container to 4Gi.",
            "Rolling-restart the deployment to pick up the new limit.",
        ],
        "commands": [
            "kubectl set resources deployment/worker --limits=memory=4Gi",
            "kubectl rollout restart deployment/worker",
        ],
        "explanation": "Memory limit increase resolves the OOMKill.",
    }
    state = _make_state()

    with _force_gemini_available(True), _mock_gemini_success(legacy_payload):
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert isinstance(sr.steps, list) and len(sr.steps) >= 1
    assert isinstance(sr.commands, list) and len(sr.commands) >= 1
    assert "kubectl" in sr.commands[0]
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


# ===========================================================================
# 8–12. _parse_steps_from_playbook — command extraction strategies
# ===========================================================================

def test_backtick_commands_extracted_from_prose():
    """
    Strategy 2: commands embedded in backtick notation within bullet prose
    must be extracted even though the bullet text itself starts with regular
    English (not a command keyword).
    """
    playbook = (
        "[Playbook: OOM Test]\n"
        "Severity: Critical\n\n"
        "Resolution steps:\n"
        "  • Verify status using `kubectl get pod worker-api -o yaml`.\n"
        "  • Increase the `resources.limits.memory` in the manifest.\n"
        "  • Roll out using `kubectl rollout restart deployment/worker-api`.\n"
    )
    steps, commands = _parse_steps_from_playbook(playbook)

    assert len(steps) == 3, "All three bullet lines must be captured as steps"
    assert any("kubectl get pod" in c for c in commands), (
        "kubectl get pod must be extracted from backtick prose"
    )
    assert any("kubectl rollout restart" in c for c in commands), (
        "kubectl rollout restart must be extracted from backtick prose"
    )
    # A YAML-key backtick like `resources.limits.memory` must NOT become a command
    assert not any("resources.limits.memory" in c for c in commands), (
        "YAML-key backtick must not be captured as a command"
    )


def test_code_fix_standalone_commands_extracted():
    """
    Strategy 3: standalone kubectl/systemctl lines in code_fix sections
    (not preceded by a bullet) must be captured as commands.
    """
    playbook = (
        "[Playbook: Memory Leak]\n"
        "Severity: Critical\n\n"
        "Resolution steps:\n"
        "  • Identify the leak by reviewing memory metrics.\n\n"
        "Code fix:\n"
        "kubectl set resources deployment/my-service -c app --limits=memory=2Gi\n"
    )
    steps, commands = _parse_steps_from_playbook(playbook)

    assert len(steps) == 1
    assert len(commands) == 1
    assert commands[0].startswith("kubectl set resources")


def test_commands_deduplicated():
    """
    The same command string appearing multiple times must appear exactly once
    in the returned commands list.
    """
    playbook = (
        "[Playbook: Dedup Test]\n\n"
        "Resolution steps:\n"
        "  • Check pods with `kubectl get pods`.\n"
        "  • Also check pods: `kubectl get pods`.\n"
    )
    _, commands = _parse_steps_from_playbook(playbook)

    assert commands.count("kubectl get pods") == 1, (
        "Duplicate command must appear only once"
    )


def test_hdfs_commands_extracted():
    """
    Strategy 2: HDFS commands embedded in backtick prose must be recognised
    and extracted — generalised tooling support beyond just kubectl.
    """
    playbook = (
        "[Playbook: HDFS Corruption]\n\n"
        "Resolution steps:\n"
        "  • Run `hdfs dfsadmin -report` to check cluster health.\n"
        "  • Repair corruption using `hdfs fsck / -delete`.\n"
    )
    _, commands = _parse_steps_from_playbook(playbook)

    assert any("hdfs dfsadmin" in c for c in commands), (
        "hdfs dfsadmin must be extracted"
    )
    assert any("hdfs fsck" in c for c in commands), (
        "hdfs fsck must be extracted"
    )


def test_iptables_and_systemctl_commands_extracted():
    """
    Strategy 2: iptables and systemctl commands embedded in backtick prose
    must be extracted — covers the SSH bruteforce and service-restart scenarios.
    """
    playbook = (
        "[Playbook: SSH Bruteforce]\n\n"
        "Resolution steps:\n"
        "  • Block attacker: `iptables -A INPUT -s 10.0.0.1 -j DROP`.\n"
        "  • Harden SSH: `systemctl restart sshd`.\n"
    )
    _, commands = _parse_steps_from_playbook(playbook)

    assert any("iptables" in c for c in commands), (
        "iptables command must be extracted"
    )
    assert any("systemctl restart" in c for c in commands), (
        "systemctl command must be extracted"
    )


def test_playbook_fallback_populates_commands():
    """
    End-to-end: when Gemini fails and _playbook_fallback() is used,
    solution_result.commands must be non-empty for playbooks whose steps
    contain backtick-embedded commands.
    """
    backtick_rag = RagResult(
        playbook_steps=(
            "[Playbook: OOMKilled — Memory Limit]\n"
            "Severity: Critical\n\n"
            "Resolution steps:\n"
            "  • Check limits: `kubectl describe pod worker-api`.\n"
            "  • Increase limit: `kubectl set resources deployment/worker-api "
            "--limits=memory=512Mi`.\n"
            "  • Restart: `kubectl rollout restart deployment/worker-api`.\n"
        ),
        rag_confidence=0.80,
    )
    state = _make_state(rag=backtick_rag)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert sr.source == "playbook_fallback"
    assert len(sr.commands) >= 1, (
        "Playbook fallback must populate commands from backtick-embedded "
        "kubectl commands in step prose"
    )
    assert any("kubectl" in c for c in sr.commands)


# ===========================================================================
# 13. Robust LLM JSON parsing tests
# ===========================================================================

def test_parse_llm_response_robustness():
    """
    Ensure _parse_llm_response is robust against:
    - Plain JSON
    - JSON wrapped in markdown fences
    - Multi-line JSON
    - Explanatory text before/after it
    - Invalid JSON handling (ValueError raised)
    """
    import pytest

    # 1. Plain JSON string
    plain_json = """{
        "root_cause": "Weak SSH config allowed brute-force attempts.",
        "commands": [
            {"cmd": "systemctl restart sshd", "risk": "warning", "description": "Restart sshd"}
        ]
    }"""
    steps, cmds, explanation = _parse_llm_response(plain_json)
    assert steps == ["Restart sshd"]
    assert cmds == ["systemctl restart sshd"]
    assert explanation == "Weak SSH config allowed brute-force attempts."

    # 2. JSON wrapped in markdown fences (with/without json specifier)
    fenced_json = """```json
    {
        "root_cause": "Weak SSH config allowed brute-force attempts.",
        "commands": [
            {"cmd": "systemctl restart sshd", "risk": "warning", "description": "Restart sshd"}
        ]
    }
    ```"""
    steps, cmds, explanation = _parse_llm_response(fenced_json)
    assert steps == ["Restart sshd"]
    assert cmds == ["systemctl restart sshd"]
    assert explanation == "Weak SSH config allowed brute-force attempts."

    # 3. Multi-line JSON with actual newlines inside quotes
    multiline_json = """{
        "root_cause": "Weak SSH configuration\\nallowed brute-force attempts.",
        "commands": [
            {
                "cmd": "systemctl restart sshd",
                "risk": "warning",
                "description": "Restart\\nsshd"
            }
        ]
    }"""
    steps, cmds, explanation = _parse_llm_response(multiline_json)
    assert steps == ["Restart\nsshd"]
    assert cmds == ["systemctl restart sshd"]
    assert explanation == "Weak SSH configuration\nallowed brute-force attempts."

    # 4. JSON with explanatory text before/after it
    explanatory_json = """Here is the remediation plan:

```json
{
    "root_cause": "Weak SSH configuration allowed brute-force attempts.",
    "commands": [
        {"cmd": "systemctl restart sshd", "risk": "warning", "description": "Restart sshd"}
    ]
}
```

Please run these commands as root."""
    steps, cmds, explanation = _parse_llm_response(explanatory_json)
    assert steps == ["Restart sshd"]
    assert cmds == ["systemctl restart sshd"]
    assert explanation == "Weak SSH configuration allowed brute-force attempts."

    # 5. Nested curly braces in preamble/postamble
    nested_braces_json = """The system has {some} issues.
Here is the JSON:
{
    "root_cause": "Weak SSH configuration allowed brute-force attempts.",
    "commands": [
        {"cmd": "systemctl restart sshd", "risk": "warning", "description": "Restart sshd"}
    ]
}
Please resolve {immediately} if possible."""
    steps, cmds, explanation = _parse_llm_response(nested_braces_json)
    assert steps == ["Restart sshd"]
    assert cmds == ["systemctl restart sshd"]
    assert explanation == "Weak SSH configuration allowed brute-force attempts."

    # 6. Invalid JSON handling (raises ValueError)
    invalid_json = "This is not JSON at all, even with { braces }."
    with pytest.raises(ValueError, match="No JSON object found"):
        _parse_llm_response(invalid_json)


# ===========================================================================
# 14. Gemini fenced-JSON extraction — direct unit tests of _extract_and_parse_json
# ===========================================================================

from ai_core.workflow.agents.solution_agent import _extract_and_parse_json


def test_extract_json_gemini_realistic_fenced_response():
    """
    Replicates the exact response format that was causing
    'No JSON object found in Gemini response' errors in production:
    a full JSON object wrapped in ```json ... ``` fences.
    The fence-stripping regex must handle this before falling through
    to the bracket-scan fallback.
    """
    gemini_response = (
        "```json\n"
        "{\n"
        '  "root_cause": "The container exceeded its 256Mi memory limit due to unbounded cache growth.",\n'
        '  "commands": [\n'
        "    {\n"
        '      "cmd": "kubectl set resources deployment/worker-api --limits=memory=512Mi -n production",\n'
        '      "risk": "warning",\n'
        '      "description": "Double the memory limit to prevent immediate OOMKills."\n'
        "    },\n"
        "    {\n"
        '      "cmd": "kubectl rollout restart deployment/worker-api -n production",\n'
        '      "risk": "warning",\n'
        '      "description": "Rolling-restart to apply the new resource limit."\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```"
    )

    result = _extract_and_parse_json(gemini_response)
    assert result["root_cause"].startswith("The container exceeded")
    assert len(result["commands"]) == 2
    assert result["commands"][0]["cmd"].startswith("kubectl set resources")
    assert result["commands"][1]["cmd"].startswith("kubectl rollout restart")


def test_extract_json_fence_without_json_specifier():
    """
    ``` (no 'json' language specifier) must also be stripped and parsed.
    This happens when Gemini omits the language tag after the opening fence.
    """
    fenced = '```\n{"root_cause": "OOM", "commands": []}\n```'
    result = _extract_and_parse_json(fenced)
    assert result["root_cause"] == "OOM"
    assert result["commands"] == []


def test_extract_json_fence_with_preamble_and_postamble():
    """
    Text before AND after the fence block must be ignored.
    This happens when Gemini prefixes with 'Here is the plan:' and
    adds a footnote like 'Please review before running.' after the block.
    """
    response = (
        "Here is my remediation plan:\n\n"
        "```json\n"
        '{"root_cause": "Memory leak", "commands": [{"cmd": "kubectl top pod", "risk": "safe", "description": "Check metrics"}]}\n'
        "```\n\n"
        "Please review carefully before executing."
    )
    result = _extract_and_parse_json(response)
    assert result["root_cause"] == "Memory leak"
    assert len(result["commands"]) == 1
    assert result["commands"][0]["cmd"] == "kubectl top pod"


# ===========================================================================
# 15. Narrative command filtering — _parse_steps_from_playbook
# ===========================================================================

def test_narrative_steps_not_extracted_as_commands():
    """
    Playbook bullets that START with English narrative words that happen to
    be kubectl sub-commands (Apply, Delete, Get, Describe, Exec, Logs, Patch)
    must NOT appear in the commands list.

    Only backtick-embedded kubectl commands and lines beginning with actual
    executable names (kubectl, docker, systemctl, …) must be extracted.
    """
    narrative_playbook = (
        "[Playbook: OOM Recovery]\n\n"
        "Resolution steps:\n"
        "  * Apply the updated Kubernetes manifest to trigger a rolling restart.\n"
        "  * Delete the old crashlooping pods to force rescheduling.\n"
        "  * Get the current resource usage metrics from the monitoring stack.\n"
        "  * Describe each affected pod to check the OOMKilled event history.\n"
        "  * Use `kubectl top pod -n production` to verify memory pressure.\n"
        "  * Execute `kubectl rollout restart deployment/worker-api` to redeploy.\n"
    )
    steps, commands = _parse_steps_from_playbook(narrative_playbook)

    # All 6 bullets must be captured as steps
    assert len(steps) == 6, f"Expected 6 steps, got {len(steps)}: {steps}"

    # Only the two backtick-embedded kubectl commands must be in commands
    assert len(commands) == 2, (
        f"Expected 2 commands (backtick-extracted kubectl), got {len(commands)}: {commands}"
    )
    assert any("kubectl top pod" in c for c in commands)
    assert any("kubectl rollout restart" in c for c in commands)

    # Narrative lines must NOT have leaked into commands
    narrative_starts = ("Apply the", "Delete the", "Get the", "Describe each")
    for start in narrative_starts:
        assert not any(c.startswith(start) for c in commands), (
            f"Narrative text starting with '{start}' must not appear in commands"
        )


def test_playbook_with_only_narrative_returns_empty_commands_and_warning():
    """
    A playbook containing exclusively narrative guidance (no kubectl / docker /
    systemctl lines, no backtick-embedded commands) must produce:
    - commands == []
    - explanation containing 'no executable commands'
    """
    narrative_only_rag = RagResult(
        playbook_steps=(
            "[Playbook: Generic Incident Response]\n\n"
            "Resolution steps:\n"
            "  * Analyze the logs carefully to understand the failure mode.\n"
            "  * Increase the memory limit by editing the Deployment manifest.\n"
            "  * Apply the changes after peer review.\n"
            "  * Monitor the metrics dashboard for the next 30 minutes.\n"
            "  * Escalate to the platform team if the issue persists.\n"
        ),
        rag_confidence=0.65,
    )
    state = _make_state(rag=narrative_only_rag)

    with _force_gemini_available(True), _mock_gemini_fail():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert sr.source == "playbook_fallback"
    assert sr.commands == [], (
        f"No executable commands should be extracted from narrative-only playbook, got: {sr.commands}"
    )
    assert "no executable commands" in sr.explanation.lower(), (
        f"Explanation must warn about missing commands, got: {sr.explanation!r}"
    )

