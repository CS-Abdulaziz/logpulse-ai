"""
ai_core/simulation/state_manager.py — Mutates ClusterState in response to AI commands.
"""
from __future__ import annotations

from typing import Optional

from sim_models import (
    ClusterState,
    DeploymentStatus,
    PodStatus,
    SimulatedPod,
)


class StateManager:
    """Applies verified command effects to a ClusterState in-place."""

    def __init__(self, state: ClusterState) -> None:
        self._state = state

    # ------------------------------------------------------------------
    # Kubernetes operations
    # ------------------------------------------------------------------

    def restart_deployment(self, deployment_name: str) -> str:
        """Simulate `kubectl rollout restart deployment/<name>`."""
        dep = self._state.deployments.get(deployment_name)
        if dep is None:
            return f"Error from server (NotFound): deployments.apps \"{deployment_name}\" not found"

        dep.status = DeploymentStatus.PROGRESSING

        for pod_name in list(dep.pods):
            pod = self._state.pods.get(pod_name)
            if pod is None:
                continue
            old_status = pod.status
            pod.status   = PodStatus.RUNNING
            pod.restarts += 1
            pod.ready    = "1/1"
            if old_status == PodStatus.OOMKILLED:
                pod.memory_usage = "64Mi"

        dep.status = DeploymentStatus.AVAILABLE
        dep.ready  = dep.replicas
        return f'deployment.apps/{deployment_name} restarted'

    def set_memory_limit(self, deployment_name: str, limit: str) -> str:
        """Simulate `kubectl set resources deployment/<name> --limits=memory=<X>`."""
        dep = self._state.deployments.get(deployment_name)
        if dep is None:
            return f"Error from server (NotFound): deployments.apps \"{deployment_name}\" not found"

        for pod_name in dep.pods:
            pod = self._state.pods.get(pod_name)
            if pod:
                pod.memory_limit = limit

        return f"deployment.apps/{deployment_name} resource requirements updated"

    def delete_pod(self, pod_name: str, namespace: str = "production") -> str:
        """Simulate `kubectl delete pod <name>`."""
        pod = self._state.pods.get(pod_name)
        if pod is None or pod.namespace != namespace:
            return f'Error from server (NotFound): pods "{pod_name}" not found'
        pod.status   = PodStatus.PENDING
        pod.restarts += 1
        pod.ready    = "0/1"
        return f'pod "{pod_name}" deleted'

    # ------------------------------------------------------------------
    # SSH / security operations
    # ------------------------------------------------------------------

    def block_ip(self, ip: str) -> str:
        """Simulate `iptables -A INPUT -s <ip> -j DROP`."""
        if self._state.ssh is None:
            return "iptables: no SSH state available"
        if ip not in self._state.ssh.blocked_ips:
            self._state.ssh.blocked_ips.append(ip)
            self._state.ssh.active_connections = 0
        return ""

    def unlock_account(self, account: str) -> str:
        """Simulate `faillock --user <account> --reset`."""
        if self._state.ssh is None:
            return "faillock: no SSH state available"
        if account in self._state.ssh.locked_accounts:
            self._state.ssh.locked_accounts.remove(account)
        return f"User {account} unlocked"

    # ------------------------------------------------------------------
    # HDFS operations
    # ------------------------------------------------------------------

    def restore_hdfs_block(self, block_id: str) -> str:
        """Simulate `hdfs fsck /` or replica restoration."""
        if self._state.hdfs is None:
            return "HDFS: no state available"
        for block in self._state.hdfs.corrupt_blocks:
            if block.block_id == block_id:
                block.corrupt  = False
                block.replicas = 3
                self._state.hdfs.missing_blocks = max(
                    0, self._state.hdfs.missing_blocks - 1
                )
                if not any(b.corrupt for b in self._state.hdfs.corrupt_blocks):
                    self._state.hdfs.healthy = True
                return f"Block {block_id} re-replicated successfully"
        return f"Block {block_id} not found"

    def run_hdfs_fsck(self) -> str:
        """Simulate `hdfs fsck /`."""
        if self._state.hdfs is None:
            return "HDFS not available"
        corrupt = sum(1 for b in self._state.hdfs.corrupt_blocks if b.corrupt)
        missing = self._state.hdfs.missing_blocks
        lines = [
            f"FSCK started by root from /default for path / at {__import__('datetime').datetime.now()}",
            f"Status: {'HEALTHY' if self._state.hdfs.healthy else 'CORRUPT'}",
            f"Total size: {self._state.hdfs.total_blocks * 128} MB",
            f"Total blocks: {self._state.hdfs.total_blocks} (avg. block size 128 MB)",
            f"Corrupt blocks: {corrupt}",
            f"Missing replicas: {missing}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # BlueGene operations
    # ------------------------------------------------------------------

    def restore_bluegene_node(self, node_name: str) -> str:
        """Simulate bringing a BlueGene node back online."""
        if self._state.bluegene is None:
            return "BlueGene state not available"
        for node in self._state.bluegene.nodes:
            if node.name == node_name:
                if node.online:
                    return f"Node {node_name} is already online"
                node.online          = True
                node.status          = "Ready"
                node.hardware_faults = 0
                node.temperature     = 65.0
                return f"Node {node_name} brought online, diagnostics passed"
        return f"Node {node_name} not found"

    # ------------------------------------------------------------------
    # System operations
    # ------------------------------------------------------------------

    def get_free_memory(self) -> str:
        """Simulate `free -h`."""
        total   = self._state.free_mem_mb + 512
        used    = total - self._state.free_mem_mb
        shared  = 16
        cache   = 128
        avail   = self._state.free_mem_mb
        lines = [
            "              total        used        free      shared  buff/cache   available",
            f"Mem:        {total:>6}M     {used:>6}M     {avail:>6}M     {shared:>6}M     {cache:>6}M     {avail:>6}M",
            f"Swap:            0M          0M          0M",
        ]
        return "\n".join(lines)

    @property
    def state(self) -> ClusterState:
        return self._state
