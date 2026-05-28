"""
ai_core/simulation/renderers.py — kubectl-like output renderers for ClusterState.
"""
from __future__ import annotations

from typing import Optional

from sim_models import ClusterState, PodStatus


def render_pods(state: ClusterState, namespace: Optional[str] = None) -> str:
    """Render `kubectl get pods` style table."""
    ns = namespace or "production"
    pods = [p for p in state.pods.values() if p.namespace == ns]
    if not pods:
        return f"No resources found in {ns} namespace."

    lines = ["NAME                          READY   STATUS             RESTARTS   AGE"]
    for pod in pods:
        status_str = pod.status.value
        line = (
            f"{pod.name:<30}{pod.ready:<8}{status_str:<19}"
            f"{pod.restarts:<11}{pod.age}"
        )
        lines.append(line)
    return "\n".join(lines)


def render_pod_details(state: ClusterState, pod_name: str) -> str:
    """Render `kubectl describe pod <name>` style output."""
    pod = state.pods.get(pod_name)
    if pod is None:
        return f'Error from server (NotFound): pods "{pod_name}" not found'

    lines = [
        f"Name:         {pod.name}",
        f"Namespace:    {pod.namespace}",
        f"Node:         {pod.node}",
        f"Status:       {pod.status.value}",
        f"Containers:",
        f"  app:",
        f"    Image:    app:latest",
        f"    Limits:",
        f"      memory: {pod.memory_limit}",
        f"    Usage:",
        f"      memory: {pod.memory_usage}",
        f"    Last State:",
        f"      Reason:    {pod.status.value}",
        f"      Exit Code: {'137' if pod.status == PodStatus.OOMKILLED else '0'}",
        f"Events:",
    ]
    if pod.status == PodStatus.OOMKILLED:
        lines.append(
            f"  Warning  OOMKilling  Container app exceeded memory limit"
        )
    return "\n".join(lines)


def render_pod_logs(state: ClusterState, pod_name: str) -> str:
    """Render `kubectl logs <name>` style output."""
    pod = state.pods.get(pod_name)
    if pod is None:
        return f'Error from server (NotFound): pods "{pod_name}" not found'

    if pod.status == PodStatus.RUNNING:
        return (
            f"2025-01-15T22:14:07Z INFO  {pod.name} started successfully\n"
            f"2025-01-15T22:14:08Z INFO  Listening on :8080\n"
            f"2025-01-15T22:14:10Z INFO  Health check passed"
        )
    if pod.status == PodStatus.OOMKILLED:
        return (
            f"2025-01-15T22:10:00Z WARN  Memory usage rising: 78% of {pod.memory_limit}\n"
            f"2025-01-15T22:13:55Z ERROR Memory critical: 94% of {pod.memory_limit}\n"
            f"2025-01-15T22:14:07Z FATAL OOMKilled: container exceeded {pod.memory_limit}"
        )
    return f"2025-01-15T22:14:07Z INFO  {pod.name}: no recent logs"


def render_nodes(state: ClusterState) -> str:
    """Render `kubectl get nodes` style table."""
    if not state.nodes:
        return "No nodes found."
    lines = ["NAME            STATUS   ROLES    AGE   VERSION"]
    for node in state.nodes.values():
        status = "Ready" if node.online else "NotReady"
        line = (
            f"{node.name:<16}{status:<9}{node.roles:<9}"
            f"{node.age:<6}{node.version}"
        )
        lines.append(line)
    return "\n".join(lines)


def render_deployments(state: ClusterState, namespace: Optional[str] = None) -> str:
    """Render `kubectl get deployments` style table."""
    ns = namespace or "production"
    deps = [d for d in state.deployments.values() if d.namespace == ns]
    if not deps:
        return f"No resources found in {ns} namespace."

    lines = ["NAME             READY   UP-TO-DATE   AVAILABLE   AGE"]
    for dep in deps:
        line = (
            f"{dep.name:<17}{dep.ready}/{dep.replicas:<7}"
            f"{dep.replicas:<13}{dep.ready:<12}2d"
        )
        lines.append(line)
    return "\n".join(lines)


def render_service_status(service_name: str, active: bool = True) -> str:
    """Render `systemctl status <service>` style output."""
    state_str  = "active (running)" if active else "inactive (dead)"
    loaded_str = f"loaded (/lib/systemd/system/{service_name}.service; enabled)"
    lines = [
        f"  {service_name}.service - {service_name.upper()} Service",
        f"     Loaded: {loaded_str}",
        f"     Active: {state_str}",
        f"    Process: ExecStart=/usr/sbin/{service_name} -D (pid 1234)",
        f"   Main PID: 1234 ({service_name})",
    ]
    return "\n".join(lines)


def render_ssh_auth_log(state: ClusterState) -> str:
    """Render recent /var/log/auth.log tail for SSH scenario."""
    if state.ssh is None:
        return "auth.log not available"
    ip  = state.ssh.attacker_ip or "10.0.0.55"
    cnt = state.ssh.failed_attempts
    lines = [
        f"Jan 15 22:10:01 server sshd[1234]: Failed password for root from {ip} port 49821 ssh2",
        f"Jan 15 22:10:02 server sshd[1234]: Failed password for admin from {ip} port 49822 ssh2",
        f"Jan 15 22:10:03 server sshd[1234]: Failed password for root from {ip} port 49823 ssh2",
        f"Jan 15 22:10:04 server sshd[1234]: message repeated {cnt} times: [ Failed password ]",
    ]
    if state.ssh.locked_accounts:
        for acc in state.ssh.locked_accounts:
            lines.append(
                f"Jan 15 22:14:07 server sshd[1234]: pam_unix(sshd:auth): "
                f"authentication failure; user={acc} rhost={ip}"
            )
    return "\n".join(lines)
