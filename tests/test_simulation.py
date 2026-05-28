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

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT           = Path(__file__).parent.parent
_SIMULATION_DIR = str(_ROOT / "ai_core" / "simulation")

if _SIMULATION_DIR not in sys.path:
    sys.path.insert(0, _SIMULATION_DIR)

from sim_models import (          # noqa: E402
    ClusterState,
    PodStatus,
    DeploymentStatus,
)
from state_manager import StateManager  # noqa: E402
from command_router import route        # noqa: E402

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
