"""
tests/test_context_resolver.py
================================
Tests for ai_core/workflow/agents/context_resolver.py

Coverage
--------
Entity extraction
  1.  OOMKilled log — pod name, deployment name, namespace, memory limit
  2.  SSH brute-force log — attacker IP, locked account, service name
  3.  cluster_state overrides log-extracted values (highest confidence)
  4.  cluster_state SSH fields (attacker_ip, locked_account)
  5.  Deployment name stripped from pod name with K8s hash suffix
  6.  Node name extracted from "on node worker-3" pattern
  7.  Namespace extracted from -n flag in log
  8.  Safe-value filter rejects shell metacharacters
  9.  Missing / None state fields degrade gracefully

Placeholder resolution
 10.  OOMKilled: <deployment-name> → worker-api
 11.  OOMKilled: <pod-name> → full pod name from cluster_state
 12.  SSH: <ip> → 10.0.0.55, <account> → root
 13.  Alias variants (<deployment_name>, <deploymentname>) all resolve
 14.  Unrecognised placeholder <manifest-file> left unchanged
 15.  Placeholder with no confident value left unchanged
 16.  resolve_placeholders never raises on malformed input
 17.  Commands without any placeholders are returned unchanged

Integration with solution_agent_node
 18.  Playbook fallback: resolved commands contain real names (OOM scenario)
 19.  Playbook fallback: commands are executable in simulation (exit_code=0)
 20.  SSH brute-force: resolved iptables command blocks attacker IP
 21.  SSH brute-force: resolved faillock command unlocks account

Simulation state mutation
 22.  After executing resolved kubectl rollout restart, pod status → Running
 23.  After executing resolved kubectl set resources, pod memory limit updated
 24.  After executing resolved iptables DROP, IP appears in blocked_ips

Run from project root:
    pytest tests/test_context_resolver.py -v
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

from ai_core.workflow.agents.context_resolver import (
    IncidentContext,
    extract_context,
    resolve_placeholders,
    _strip_k8s_hash,
    _safe,
)
from ai_core.workflow.agents import solution_agent
from ai_core.workflow.agents.solution_agent import solution_agent_node
from ai_core.workflow.state import (
    ClassificationData,
    DiagnosticResult,
    LogState,
    RagResult,
    SeverityLevel,
    SolutionResult,
)
from ai_core.simulation.sim_models import ClusterState, PodStatus, DeploymentStatus
from ai_core.simulation.command_router import route
from ai_core.simulation.state_manager import StateManager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_SCENARIOS = _ROOT / "data" / "scenarios"


def _load_cluster_state(name: str) -> ClusterState:
    path = _SCENARIOS / f"{name}_state.json"
    return ClusterState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _make_oom_state(with_cluster: bool = False) -> LogState:
    cs = _load_cluster_state("oom_critical") if with_cluster else None
    return LogState(
        raw_log="OOMKilled: pod/worker-api exceeded memory limit 256Mi restarts=14",
        classification=ClassificationData(
            category="Memory",
            source="Kubernetes",
            severity=SeverityLevel.FATAL,
            summary="Pod OOMKilled.",
        ),
        diagnostic_result=DiagnosticResult(
            root_cause="worker-api exceeded 256Mi memory limit, OOM killed.",
            confidence=0.90,
            reasoning="OOMKilled log.",
            used_history=False,
            used_playbook=True,
            source="llm",
        ),
        rag_result=RagResult(
            playbook_steps=(
                "[Playbook: Pod OOMKilled]\n"
                "Severity: Critical\n\n"
                "Resolution steps:\n"
                "  • Verify status: `kubectl get pod <pod-name> -o yaml`.\n"
                "  • Increase limit: `kubectl set resources deployment/<deployment-name>"
                " --limits=memory=512Mi`.\n"
                "  • Restart: `kubectl rollout restart deployment/<deployment-name>`.\n"
            ),
            rag_confidence=0.75,
        ),
        cluster_state=cs,
    )


def _make_ssh_state(with_cluster: bool = False) -> LogState:
    cs = _load_cluster_state("ssh_bruteforce") if with_cluster else None
    return LogState(
        raw_log=(
            "sshd: Too many authentication failures for root "
            "from 10.0.0.55 port 49900 ssh2 — account locked"
        ),
        classification=ClassificationData(
            category="Security",
            source="Linux",
            severity=SeverityLevel.FATAL,
            summary="SSH brute force.",
        ),
        rag_result=RagResult(
            playbook_steps=(
                "[Playbook: SSH Bruteforce]\n\n"
                "Resolution steps:\n"
                "  • Block attacker: `iptables -A INPUT -s <ip> -j DROP`.\n"
                "  • Unlock account: `faillock --user <account> --reset`.\n"
                "  • Restart SSH: `systemctl restart sshd`.\n"
            ),
            rag_confidence=0.80,
        ),
        cluster_state=cs,
    )


def _force_gemini(available: bool = False):
    return mock.patch.object(solution_agent, "GEMINI_AVAILABLE", available)


def _fail_gemini():
    return mock.patch(
        "ai_core.workflow.agents.solution_agent._call_gemini",
        side_effect=RuntimeError("Simulated Gemini failure"),
    )


# ===========================================================================
# 1. OOMKilled log — basic entity extraction
# ===========================================================================

def test_extract_oom_pod_and_deployment():
    state = _make_oom_state()
    ctx   = extract_context(state)

    assert ctx.pod_name        == "worker-api", f"pod_name={ctx.pod_name}"
    assert ctx.deployment_name == "worker-api", f"deployment_name={ctx.deployment_name}"
    assert ctx.memory_limit    == "256Mi",      f"memory_limit={ctx.memory_limit}"


def test_extract_oom_namespace_defaults_to_production():
    state = _make_oom_state()
    ctx   = extract_context(state)
    assert ctx.namespace == "production"


# ===========================================================================
# 2. SSH brute-force log — attacker IP, account, service
# ===========================================================================

def test_extract_ssh_attacker_ip():
    state = _make_ssh_state()
    ctx   = extract_context(state)
    assert ctx.attacker_ip == "10.0.0.55", f"attacker_ip={ctx.attacker_ip}"


def test_extract_ssh_locked_account():
    state = _make_ssh_state()
    ctx   = extract_context(state)
    assert ctx.locked_account == "root", f"locked_account={ctx.locked_account}"


def test_extract_ssh_service_name():
    state = _make_ssh_state()
    ctx   = extract_context(state)
    assert ctx.service_name == "sshd", f"service_name={ctx.service_name}"


# ===========================================================================
# 3. cluster_state overrides log values
# ===========================================================================

def test_cluster_state_deployment_name_wins():
    """cluster_state.deployments provides ground-truth deployment name."""
    state = _make_oom_state(with_cluster=True)
    ctx   = extract_context(state)
    # cluster_state has deployment "worker-api"
    assert ctx.deployment_name == "worker-api"


def test_cluster_state_pod_name_is_full_hash_name():
    """cluster_state provides the full hashed pod name, not a stripped version."""
    state = _make_oom_state(with_cluster=True)
    ctx   = extract_context(state)
    # cluster_state has pod "worker-api-7d9f8b-xk2q"
    assert ctx.pod_name == "worker-api-7d9f8b-xk2q", f"pod_name={ctx.pod_name}"


def test_cluster_state_node_name():
    state = _make_oom_state(with_cluster=True)
    ctx   = extract_context(state)
    assert ctx.node_name == "worker-3", f"node_name={ctx.node_name}"


# ===========================================================================
# 4. cluster_state SSH fields
# ===========================================================================

def test_cluster_state_ssh_attacker_ip():
    state = _make_ssh_state(with_cluster=True)
    ctx   = extract_context(state)
    assert ctx.attacker_ip    == "10.0.0.55"
    assert ctx.locked_account == "root"


# ===========================================================================
# 5. K8s hash suffix stripping
# ===========================================================================

def test_strip_k8s_hash_removes_two_segments():
    assert _strip_k8s_hash("worker-api-7d9f8b-xk2q") == "worker-api"


def test_strip_k8s_hash_removes_one_segment():
    assert _strip_k8s_hash("nginx-7c9f5b") == "nginx"


def test_strip_k8s_hash_leaves_clean_name():
    assert _strip_k8s_hash("worker-api") == "worker-api"


def test_strip_k8s_hash_leaves_version_tag():
    # "v2" is only 2 chars — below the minimum length (4) → not stripped
    assert _strip_k8s_hash("worker-api-v2") == "worker-api-v2"


def test_strip_k8s_hash_leaves_word_suffix():
    # "-deployment" has no digits → not stripped
    assert _strip_k8s_hash("nginx-deployment") == "nginx-deployment"


# ===========================================================================
# 6. Node name extraction from log
# ===========================================================================

def test_extract_node_from_log():
    state = LogState(raw_log="worker-api pod started successfully on node worker-3")
    ctx   = extract_context(state)
    assert ctx.node_name == "worker-3"


# ===========================================================================
# 7. Namespace extraction
# ===========================================================================

def test_extract_namespace_from_n_flag():
    state = LogState(raw_log="kubectl get pods -n kube-system pod/coredns OOMKilled")
    ctx   = extract_context(state)
    assert ctx.namespace == "kube-system"


# ===========================================================================
# 8. Safety filter
# ===========================================================================

def test_safe_filter_rejects_semicolon():
    assert _safe("worker; rm -rf /") is None


def test_safe_filter_rejects_dollar():
    assert _safe("$(evil)") is None


def test_safe_filter_accepts_clean_names():
    assert _safe("worker-api")            == "worker-api"
    assert _safe("worker-api-7d9f8b")     == "worker-api-7d9f8b"
    assert _safe("10.0.0.55")             == "10.0.0.55"
    assert _safe("256Mi")                 == "256Mi"
    assert _safe("production")            == "production"


# ===========================================================================
# 9. Graceful degradation — missing fields
# ===========================================================================

def test_extract_minimal_state_no_crash():
    """extract_context must not raise even with no optional fields set."""
    state = LogState(raw_log="some generic log line without any entities")
    ctx   = extract_context(state)
    assert isinstance(ctx, IncidentContext)
    assert ctx.pod_name is None
    assert ctx.attacker_ip is None
    assert ctx.namespace == "production"   # default


# ===========================================================================
# 10. OOMKilled placeholder resolution — deployment name
# ===========================================================================

def test_resolve_deployment_name():
    ctx = IncidentContext(deployment_name="worker-api")
    resolved = resolve_placeholders(
        ["kubectl rollout restart deployment/<deployment-name>"],
        ctx,
    )
    assert resolved == ["kubectl rollout restart deployment/worker-api"]


def test_resolve_deployment_name_in_set_resources():
    ctx = IncidentContext(deployment_name="worker-api")
    resolved = resolve_placeholders(
        ["kubectl set resources deployment/<deployment-name> --limits=memory=512Mi"],
        ctx,
    )
    assert resolved[0] == "kubectl set resources deployment/worker-api --limits=memory=512Mi"


# ===========================================================================
# 11. OOMKilled: <pod-name> → full pod name from cluster_state
# ===========================================================================

def test_resolve_pod_name_full_hash():
    ctx = IncidentContext(pod_name="worker-api-7d9f8b-xk2q", deployment_name="worker-api")
    resolved = resolve_placeholders(
        ["kubectl get pod <pod-name> -o yaml"],
        ctx,
    )
    assert resolved == ["kubectl get pod worker-api-7d9f8b-xk2q -o yaml"]


# ===========================================================================
# 12. SSH: <ip> and <account> resolved
# ===========================================================================

def test_resolve_ip_and_account():
    ctx = IncidentContext(attacker_ip="10.0.0.55", locked_account="root")
    cmds = [
        "iptables -A INPUT -s <ip> -j DROP",
        "faillock --user <account> --reset",
    ]
    resolved = resolve_placeholders(cmds, ctx)
    assert resolved[0] == "iptables -A INPUT -s 10.0.0.55 -j DROP"
    assert resolved[1] == "faillock --user root --reset"


# ===========================================================================
# 13. Alias variants all resolve
# ===========================================================================

def test_resolve_aliases_deployment():
    ctx = IncidentContext(deployment_name="worker-api")
    variants = [
        "cmd <deployment-name>",
        "cmd <deployment_name>",
        "cmd <deploymentname>",
        "cmd <deployment>",
    ]
    for variant in variants:
        result = resolve_placeholders([variant], ctx)
        assert result == ["cmd worker-api"], f"Failed for {variant!r}: {result}"


def test_resolve_aliases_ip():
    ctx = IncidentContext(attacker_ip="10.0.0.55")
    variants = ["<ip>", "<ip-address>", "<ip_address>", "<attacker-ip>", "<source-ip>"]
    for v in variants:
        assert resolve_placeholders([v], ctx) == ["10.0.0.55"], f"Alias failed: {v}"


# ===========================================================================
# 14. Unrecognised placeholder left unchanged
# ===========================================================================

def test_unrecognised_placeholder_unchanged():
    ctx = IncidentContext(deployment_name="worker-api")
    cmd = "kubectl apply -f <manifest-file>"
    assert resolve_placeholders([cmd], ctx) == [cmd]


def test_asg_name_placeholder_unchanged():
    ctx = IncidentContext()
    cmd = "aws autoscaling update --auto-scaling-group-name <asg-name>"
    assert resolve_placeholders([cmd], ctx) == [cmd]


# ===========================================================================
# 15. No confident value → placeholder unchanged
# ===========================================================================

def test_placeholder_unchanged_when_no_value():
    ctx = IncidentContext()   # all fields None
    cmd = "kubectl rollout restart deployment/<deployment-name>"
    assert resolve_placeholders([cmd], ctx) == [cmd]


# ===========================================================================
# 16. resolve_placeholders never raises
# ===========================================================================

def test_resolve_never_raises_on_malformed():
    ctx = IncidentContext(deployment_name="worker-api")
    # Unclosed angle bracket — should not raise
    result = resolve_placeholders(["kubectl <broken"], ctx)
    assert isinstance(result, list)
    assert len(result) == 1


def test_resolve_never_raises_on_empty():
    ctx = IncidentContext()
    assert resolve_placeholders([], ctx) == []


# ===========================================================================
# 17. Commands without placeholders are returned unchanged
# ===========================================================================

def test_clean_commands_pass_through():
    ctx = IncidentContext(deployment_name="worker-api")
    cmds = [
        "kubectl get pods -n production",
        "kubectl rollout restart deployment/worker-api",
        "iptables -A INPUT -s 10.0.0.55 -j DROP",
    ]
    assert resolve_placeholders(cmds, ctx) == cmds


# ===========================================================================
# 18. Integration: solution_agent_node (playbook fallback, OOM)
#     Resolved commands contain real deployment name
# ===========================================================================

def test_solution_agent_playbook_fallback_resolves_oom():
    """
    When Gemini fails and we fall back to the playbook, the resolved
    commands must reference the real deployment name, not <deployment-name>.
    """
    state = _make_oom_state(with_cluster=True)

    with _force_gemini(False), _fail_gemini():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    assert sr.source == "playbook_fallback"
    assert len(sr.commands) >= 1, "Must have at least one command"

    # No unresolved <deployment-name> or <pod-name> should remain
    for cmd in sr.commands:
        assert "<deployment-name>" not in cmd, (
            f"Placeholder not resolved in: {cmd}"
        )
        assert "<deployment_name>" not in cmd


def test_solution_agent_playbook_fallback_commands_have_real_names():
    """Resolved commands must contain 'worker-api', not a placeholder."""
    state = _make_oom_state(with_cluster=True)

    with _force_gemini(False), _fail_gemini():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    all_cmds = " ".join(sr.commands)
    assert "worker-api" in all_cmds, (
        f"Expected 'worker-api' in commands, got: {sr.commands}"
    )


# ===========================================================================
# 19. Integration: resolved OOM commands execute in simulation (exit_code=0)
# ===========================================================================

def test_resolved_oom_commands_execute_successfully():
    """
    Resolved commands must execute in the simulation layer with exit_code=0.
    Specifically: kubectl rollout restart and kubectl set resources.
    """
    state = _make_oom_state(with_cluster=True)

    with _force_gemini(False), _fail_gemini():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    cluster_state = state.cluster_state

    restart_cmds = [c for c in sr.commands if "rollout restart" in c]
    assert len(restart_cmds) >= 1, f"No rollout restart found in: {sr.commands}"

    for cmd in restart_cmds:
        sim_result = route(cmd, cluster_state)
        assert sim_result.exit_code == 0, (
            f"Command failed (exit {sim_result.exit_code}): {cmd}\n"
            f"  stderr: {sim_result.stderr}"
        )


# ===========================================================================
# 20. SSH: resolved iptables command blocks attacker IP
# ===========================================================================

def test_resolved_ssh_iptables_blocks_ip():
    """iptables -A INPUT -s 10.0.0.55 -j DROP → cluster state mutates."""
    state = _make_ssh_state(with_cluster=True)

    with _force_gemini(False), _fail_gemini():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    cluster_state = state.cluster_state

    iptables_cmds = [c for c in sr.commands if "iptables" in c]
    assert len(iptables_cmds) >= 1, f"No iptables command found in: {sr.commands}"

    for cmd in iptables_cmds:
        sim_result = route(cmd, cluster_state)
        assert sim_result.exit_code == 0, (
            f"iptables command failed: {cmd}\n  stderr: {sim_result.stderr}"
        )

    assert "10.0.0.55" in cluster_state.ssh.blocked_ips, (
        f"IP not blocked. blocked_ips={cluster_state.ssh.blocked_ips}"
    )


# ===========================================================================
# 21. SSH: resolved faillock command unlocks account
# ===========================================================================

def test_resolved_ssh_faillock_unlocks_account():
    """faillock --user root --reset → 'root' removed from locked_accounts."""
    state = _make_ssh_state(with_cluster=True)

    with _force_gemini(False), _fail_gemini():
        result = solution_agent_node(state)

    sr: SolutionResult = result["solution_result"]
    cluster_state = state.cluster_state

    faillock_cmds = [c for c in sr.commands if "faillock" in c]
    assert len(faillock_cmds) >= 1, f"No faillock command found in: {sr.commands}"

    for cmd in faillock_cmds:
        sim_result = route(cmd, cluster_state)
        assert sim_result.exit_code == 0, (
            f"faillock command failed: {cmd}\n  stderr: {sim_result.stderr}"
        )

    assert "root" not in cluster_state.ssh.locked_accounts, (
        f"Account still locked. locked_accounts={cluster_state.ssh.locked_accounts}"
    )


# ===========================================================================
# 22. Simulation state mutation: pod status → Running after rollout restart
# ===========================================================================

def test_simulation_oom_pod_running_after_restart():
    """
    After `kubectl rollout restart deployment/worker-api`:
    - pod status changes from OOMKilled to Running
    - deployment status changes from Degraded to Available
    """
    cs  = _load_cluster_state("oom_critical")
    mgr = StateManager(cs)

    pod = cs.pods["worker-api-7d9f8b-xk2q"]
    dep = cs.deployments["worker-api"]
    assert pod.status == PodStatus.OOMKILLED
    assert dep.status == DeploymentStatus.DEGRADED

    result = route("kubectl rollout restart deployment/worker-api", cs)
    assert result.exit_code == 0, f"Unexpected failure: {result.stderr}"

    assert pod.status   == PodStatus.RUNNING,           f"status={pod.status}"
    assert dep.status   == DeploymentStatus.AVAILABLE,  f"dep_status={dep.status}"
    assert pod.restarts == 15,                           f"restarts={pod.restarts}"


# ===========================================================================
# 23. Simulation state mutation: memory limit updated after set resources
# ===========================================================================

def test_simulation_oom_memory_limit_updated():
    """
    After `kubectl set resources deployment/worker-api --limits=memory=512Mi`:
    - pod memory_limit changes to 512Mi
    """
    cs = _load_cluster_state("oom_critical")

    result = route(
        "kubectl set resources deployment/worker-api --limits=memory=512Mi",
        cs,
    )
    assert result.exit_code == 0, f"Unexpected failure: {result.stderr}"

    pod = cs.pods["worker-api-7d9f8b-xk2q"]
    assert pod.memory_limit == "512Mi", f"memory_limit={pod.memory_limit}"


# ===========================================================================
# 24. Simulation state mutation: iptables blocks IP
# ===========================================================================

def test_simulation_ssh_ip_blocked_after_iptables():
    """
    After `iptables -A INPUT -s 10.0.0.55 -j DROP`:
    - 10.0.0.55 appears in blocked_ips
    - active_connections drops to 0
    """
    cs = _load_cluster_state("ssh_bruteforce")
    assert "10.0.0.55" not in cs.ssh.blocked_ips

    result = route("iptables -A INPUT -s 10.0.0.55 -j DROP", cs)
    assert result.exit_code == 0

    assert "10.0.0.55" in cs.ssh.blocked_ips
    assert cs.ssh.active_connections == 0
