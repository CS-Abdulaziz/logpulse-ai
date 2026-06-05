"""
tests/test_simulation.py
========================
Unit tests for the LogPulse stateful simulation layer.

Tests
-----
models.py / ClusterState
  1. ClusterState loads from oom_critical_state.json correctly
  2. ClusterState loads from ssh_bruteforce_state.json correctly
  3. ClusterState loads from hdfs_corruption_state.json correctly
  4. ClusterState loads from blueGene_node_state.json correctly

state_manager.py / StateManager
  5. restart_deployment changes OOMKilled pod to Running
  6. restart_deployment returns NotFound error for unknown deployment
  7. block_ip adds IP to blocked_ips list
  8. restore_hdfs_block marks block as not corrupt

command_router.py / route
  9.  kubectl get pods returns table with pod names
 10.  kubectl rollout restart deployment/worker-api returns success and changes state
 11.  iptables -A INPUT -s 10.0.0.55 -j DROP blocks the attacker IP
 12.  unknown command returns exit_code 127

Run:
    pytest tests/test_simulation.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT           = Path(__file__).parent.parent

from ai_core.simulation.sim_models import (
    ClusterState,
    PodStatus,
    DeploymentStatus,
)
from ai_core.simulation.state_manager import StateManager
from ai_core.simulation.command_router import route

_SCENARIOS = _ROOT / "data" / "scenarios"


# ===========================================================================
# Helpers
# ===========================================================================

def _load_state(name: str) -> ClusterState:
    path = _SCENARIOS / f"{name}_state.json"
    return ClusterState.model_validate(json.loads(path.read_text(encoding="utf-8")))


# ===========================================================================
# 1 — ClusterState loads from oom_critical_state.json
# ===========================================================================

def test_cluster_state_oom_critical_loads():
    state = _load_state("oom_critical")
    assert state.scenario == "oom_critical"
    assert "worker-api-7d9f8b-xk2q" in state.pods
    pod = state.pods["worker-api-7d9f8b-xk2q"]
    assert pod.status == PodStatus.OOMKILLED
    assert pod.restarts == 14
    assert "worker-api" in state.deployments
    dep = state.deployments["worker-api"]
    assert dep.status == DeploymentStatus.DEGRADED
    assert dep.ready == 0


# ===========================================================================
# 2 — ClusterState loads from ssh_bruteforce_state.json
# ===========================================================================

def test_cluster_state_ssh_bruteforce_loads():
    state = _load_state("ssh_bruteforce")
    assert state.scenario == "ssh_bruteforce"
    assert state.ssh is not None
    assert state.ssh.failed_attempts == 847
    assert state.ssh.attacker_ip == "10.0.0.55"
    assert "root" in state.ssh.locked_accounts
    assert state.ssh.active_connections == 12


# ===========================================================================
# 3 — ClusterState loads from hdfs_corruption_state.json
# ===========================================================================

def test_cluster_state_hdfs_corruption_loads():
    state = _load_state("hdfs_corruption")
    assert state.scenario == "hdfs_corruption"
    assert state.hdfs is not None
    assert state.hdfs.total_blocks == 48291
    assert len(state.hdfs.corrupt_blocks) == 1
    block = state.hdfs.corrupt_blocks[0]
    assert block.block_id == "blk_1073741825"
    assert block.corrupt is True
    assert state.hdfs.healthy is False


# ===========================================================================
# 4 — ClusterState loads from blueGene_node_state.json
# ===========================================================================

def test_cluster_state_bluegene_loads():
    state = _load_state("blueGene_node")
    assert state.scenario == "blueGene_node"
    assert state.bluegene is not None
    assert len(state.bluegene.nodes) == 2
    offline = next(n for n in state.bluegene.nodes if not n.online)
    assert offline.name == "R00-M0-N04"
    assert offline.hardware_faults == 47
    assert offline.temperature == 87.5
    assert state.bluegene.jobs_killed == 16


# ===========================================================================
# 5 — restart_deployment changes OOMKilled pod to Running
# ===========================================================================

def test_state_manager_restart_deployment():
    state = _load_state("oom_critical")
    mgr   = StateManager(state)

    msg = mgr.restart_deployment("worker-api")
    assert "restarted" in msg

    pod = state.pods["worker-api-7d9f8b-xk2q"]
    assert pod.status == PodStatus.RUNNING
    assert pod.restarts == 15          # 14 + 1
    assert pod.ready == "1/1"

    dep = state.deployments["worker-api"]
    assert dep.status == DeploymentStatus.AVAILABLE
    assert dep.ready == dep.replicas


# ===========================================================================
# 6 — restart_deployment returns NotFound for unknown deployment
# ===========================================================================

def test_state_manager_restart_unknown_deployment():
    state = _load_state("oom_critical")
    mgr   = StateManager(state)

    msg = mgr.restart_deployment("nonexistent-service")
    assert "NotFound" in msg or "not found" in msg.lower()


# ===========================================================================
# 7 — block_ip adds IP to blocked_ips list
# ===========================================================================

def test_state_manager_block_ip():
    state = _load_state("ssh_bruteforce")
    mgr   = StateManager(state)

    assert "10.0.0.55" not in state.ssh.blocked_ips
    mgr.block_ip("10.0.0.55")
    assert "10.0.0.55" in state.ssh.blocked_ips
    # Idempotent — should not duplicate
    mgr.block_ip("10.0.0.55")
    assert state.ssh.blocked_ips.count("10.0.0.55") == 1


# ===========================================================================
# 8 — restore_hdfs_block marks block as not corrupt
# ===========================================================================

def test_state_manager_restore_hdfs_block():
    state = _load_state("hdfs_corruption")
    mgr   = StateManager(state)

    msg = mgr.restore_hdfs_block("blk_1073741825")
    assert "re-replicated" in msg or "successfully" in msg

    block = state.hdfs.corrupt_blocks[0]
    assert block.corrupt is False
    assert block.replicas == 3
    assert state.hdfs.missing_blocks == 0
    assert state.hdfs.healthy is True


# ===========================================================================
# 9 — kubectl get pods returns table with pod names
# ===========================================================================

def test_route_kubectl_get_pods():
    state  = _load_state("oom_critical")
    result = route("kubectl get pods", state)

    assert result.exit_code == 0
    assert "worker-api-7d9f8b-xk2q" in result.stdout
    assert "OOMKilled" in result.stdout


# ===========================================================================
# 10 — kubectl rollout restart changes pod state
# ===========================================================================

def test_route_kubectl_rollout_restart():
    state  = _load_state("oom_critical")
    result = route("kubectl rollout restart deployment/worker-api", state)

    assert result.exit_code == 0
    assert "restarted" in result.stdout

    # State mutation is visible
    pod = state.pods["worker-api-7d9f8b-xk2q"]
    assert pod.status == PodStatus.RUNNING


# ===========================================================================
# 11 — iptables rule blocks attacker IP
# ===========================================================================

def test_route_iptables_block_ip():
    state  = _load_state("ssh_bruteforce")
    result = route("iptables -A INPUT -s 10.0.0.55 -j DROP", state)

    assert result.exit_code == 0
    assert "10.0.0.55" in state.ssh.blocked_ips


# ===========================================================================
# 12 — unknown command returns exit_code 127
# ===========================================================================

def test_route_unknown_command():
    state  = _load_state("oom_critical")
    result = route("xyzzy --magic", state)

    assert result.exit_code == 127
    assert "not found" in result.stderr or "xyzzy" in result.stderr


# ===========================================================================
# Intent-based execution — new tests
# ===========================================================================

# 13 — kubectl logs with namespace and --tail flag → FETCH_LOGS, exit 0
def test_route_kubectl_logs_with_flags():
    state  = _load_state("oom_critical")
    result = route("kubectl logs worker-api-7d9f8b-xk2q -n production --tail=100", state)

    assert result.exit_code == 0
    assert "OOMKilled" in result.stdout or "Memory" in result.stdout


# 14 — kubectl top pod → METRICS, exit 0, shows CPU and memory columns
def test_route_kubectl_top_pod():
    state  = _load_state("oom_critical")
    result = route("kubectl top pod", state)

    assert result.exit_code == 0
    assert "CPU(cores)" in result.stdout
    assert "MEMORY(bytes)" in result.stdout
    assert "worker-api-7d9f8b-xk2q" in result.stdout


# 15 — kubectl top pod <name> → single-pod metrics
def test_route_kubectl_top_pod_named():
    state  = _load_state("oom_critical")
    result = route("kubectl top pod worker-api-7d9f8b-xk2q", state)

    assert result.exit_code == 0
    assert "worker-api-7d9f8b-xk2q" in result.stdout


# 16 — kubectl top node → METRICS for nodes, exit 0
def test_route_kubectl_top_node():
    state  = _load_state("oom_critical")
    result = route("kubectl top node", state)

    assert result.exit_code == 0
    assert "CPU(cores)" in result.stdout


# 17 — kubectl rollout status deployment/worker-api → ROLLOUT_STATUS, exit 0
def test_route_kubectl_rollout_status():
    state  = _load_state("oom_critical")
    result = route("kubectl rollout status deployment/worker-api", state)

    # Degraded deployment → waiting message, but still exit 0
    assert result.exit_code == 0
    assert "worker-api" in result.stdout


# 18 — kubectl patch deployment worker-api (no slash) → PATCH_DEPLOYMENT, exit 0
def test_route_kubectl_patch_deployment_no_slash():
    state  = _load_state("oom_critical")
    result = route('kubectl patch deployment worker-api -p \'{"spec":{"template":{"spec":{"containers":[{"name":"app","resources":{"limits":{"memory":"512Mi"}}}]}}}}\'', state)

    assert result.exit_code == 0
    assert "patched" in result.stdout


# 19 — kubectl patch deployment/worker-api with memory in JSON → mutates state
def test_route_kubectl_patch_deployment_memory():
    state  = _load_state("oom_critical")
    result = route(
        'kubectl patch deployment/worker-api -p \'{"spec":{"template":{"spec":{"containers":[{"name":"app","resources":{"limits":{"memory":"512Mi"}}}]}}}}\'',
        state,
    )
    assert result.exit_code == 0
    pod = state.pods["worker-api-7d9f8b-xk2q"]
    assert pod.memory_limit == "512Mi"


# 20 — kubectl describe deployment worker-api → DESCRIBE_DEPLOYMENT, exit 0
def test_route_kubectl_describe_deployment():
    state  = _load_state("oom_critical")
    result = route("kubectl describe deployment worker-api", state)

    assert result.exit_code == 0
    assert "worker-api" in result.stdout
    assert "Replicas" in result.stdout


# 21 — kubectl get events → GET_EVENTS, exit 0, shows OOMKilling warning
def test_route_kubectl_get_events():
    state  = _load_state("oom_critical")
    result = route("kubectl get events", state)

    assert result.exit_code == 0
    assert "OOMKilling" in result.stdout or "LAST SEEN" in result.stdout


# 22 — kubectl get svc → GET_SERVICE, exit 0
def test_route_kubectl_get_services():
    state  = _load_state("oom_critical")
    result = route("kubectl get svc", state)

    assert result.exit_code == 0
    assert "ClusterIP" in result.stdout or "worker-api" in result.stdout


# 23 — kubectl scale deployment/worker-api --replicas=3 → SCALE, mutates state
def test_route_kubectl_scale_deployment():
    state  = _load_state("oom_critical")
    result = route("kubectl scale deployment/worker-api --replicas=3", state)

    assert result.exit_code == 0
    assert "scaled" in result.stdout
    assert state.deployments["worker-api"].replicas == 3


# 24 — unknown kubectl sub-command → safe fallback, exit 0 (never exit -1)
def test_route_unknown_kubectl_never_exits_minus_one():
    state  = _load_state("oom_critical")
    result = route("kubectl wait --for=condition=ready pod/worker-api-7d9f8b-xk2q", state)

    assert result.exit_code == 0, "Unknown kubectl subcommand must not return exit -1"


# 25 — intent parser: namespace flag in the middle of the command
def test_intent_parser_namespace_extraction():
    from ai_core.simulation.intent_parser import parse_kubectl

    intent = parse_kubectl("kubectl logs worker-api -n staging --tail=50")
    assert intent.verb      == "logs"
    assert intent.target    == "worker-api"
    assert intent.namespace == "staging"
    assert intent.flags.get("tail") == 50


# 26 — intent parser: resource/name notation
def test_intent_parser_resource_prefix_notation():
    from ai_core.simulation.intent_parser import parse_kubectl

    intent = parse_kubectl("kubectl rollout status deployment/worker-api")
    assert intent.verb     == "rollout"
    assert intent.resource == "status"
    assert intent.target   == "worker-api"


# 27 — intent parser: top pod vs top node discrimination
def test_intent_parser_top_resource_type():
    from ai_core.simulation.intent_parser import parse_kubectl

    pod_intent  = parse_kubectl("kubectl top pod worker-api")
    node_intent = parse_kubectl("kubectl top node")

    assert pod_intent.verb     == "top"
    assert pod_intent.resource == "pod"
    assert node_intent.verb    == "top"
    assert node_intent.resource == "node"
