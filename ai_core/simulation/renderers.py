"""
ai_core/simulation/renderers.py — kubectl-like output renderers for ClusterState.
"""
from __future__ import annotations

from typing import Optional

from ai_core.simulation.sim_models import ClusterState, PodStatus


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


def render_top_pods(state: ClusterState, namespace: Optional[str] = None, target: str = "") -> str:
    """Render `kubectl top pod [<name>]` style output."""
    ns   = namespace or "production"
    pods = [p for p in state.pods.values() if p.namespace == ns]
    if target:
        pods = [p for p in pods if p.name == target]
    if not pods:
        return f"No resources found in {ns} namespace."

    lines = ["NAME                          CPU(cores)   MEMORY(bytes)"]
    for pod in pods:
        raw_usage = pod.memory_usage.upper()
        if "MI" in raw_usage:
            mem_bytes = f"{raw_usage}"
        else:
            mem_bytes = pod.memory_usage or "0Mi"
        raw_limit  = pod.memory_limit.upper()
        limit_val  = int(raw_limit.rstrip("MI")) if "MI" in raw_limit else 256
        usage_val  = int(raw_usage.rstrip("MI")) if "MI" in raw_usage else 0
        cpu_pct    = min(980, int(usage_val / max(limit_val, 1) * 1000))
        cpu_str    = f"{cpu_pct}m"
        lines.append(f"{pod.name:<30}{cpu_str:<13}{mem_bytes}")
    return "\n".join(lines)


def render_top_nodes(state: ClusterState, target: str = "") -> str:
    """Render `kubectl top node [<name>]` style output."""
    nodes = list(state.nodes.values())
    if target:
        nodes = [n for n in nodes if n.name == target]
    if not nodes:
        return "No nodes found."

    lines = ["NAME            CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%"]
    for node in nodes:
        cpu    = "412m"
        cpu_p  = "10%"
        mem    = "1843Mi"
        mem_p  = "47%"
        lines.append(f"{node.name:<16}{cpu:<13}{cpu_p:<7}{mem:<16}{mem_p}")
    return "\n".join(lines)


def render_events(state: ClusterState, namespace: Optional[str] = None) -> str:
    """Render `kubectl get events` style output."""
    ns   = namespace or "production"
    pods = [p for p in state.pods.values() if p.namespace == ns]
    if not pods:
        return f"No events found in {ns} namespace."

    lines = [
        "LAST SEEN   TYPE      REASON              OBJECT                        MESSAGE"
    ]
    for pod in pods:
        if pod.status == PodStatus.OOMKILLED:
            lines.append(
                f"2m          Warning   OOMKilling          pod/{pod.name:<28}"
                f"Memory limit reached — container killed"
            )
            lines.append(
                f"2m          Warning   BackOff             pod/{pod.name:<28}"
                f"Back-off restarting failed container"
            )
        elif pod.status == PodStatus.CRASHED:
            lines.append(
                f"1m          Warning   BackOff             pod/{pod.name:<28}"
                f"Back-off restarting failed container"
            )
        elif pod.status == PodStatus.RUNNING:
            lines.append(
                f"5m          Normal    Started             pod/{pod.name:<28}"
                f"Started container app"
            )
    return "\n".join(lines)


def render_services(state: ClusterState, namespace: Optional[str] = None) -> str:
    """Render `kubectl get services` style table (derived from deployment names)."""
    ns   = namespace or "production"
    deps = [d for d in state.deployments.values() if d.namespace == ns]
    lines = [
        "NAME             TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE"
    ]
    if not deps:
        lines.append(f"No resources found in {ns} namespace.")
        return "\n".join(lines)
    for i, dep in enumerate(deps):
        ip = f"10.96.{10 + i}.{20 + i}"
        lines.append(
            f"{dep.name:<17}ClusterIP   {ip:<16}<none>        8080/TCP   2d"
        )
    return "\n".join(lines)


def render_deployment_details(state: ClusterState, dep_name: str) -> str:
    """Render `kubectl describe deployment <name>` style output."""
    dep = state.deployments.get(dep_name)
    if dep is None:
        return f'Error from server (NotFound): deployments.apps "{dep_name}" not found'

    pod_names = ", ".join(dep.pods) or "<none>"
    lines = [
        f"Name:                   {dep.name}",
        f"Namespace:              {dep.namespace}",
        f"Selector:               app={dep.name}",
        f"Replicas:               {dep.replicas} desired | {dep.ready} updated | "
        f"{dep.ready} ready | {dep.replicas - dep.ready} unavailable",
        f"StrategyType:           RollingUpdate",
        f"Status:                 {dep.status.value}",
        f"Pod Template:",
        f"  Labels:  app={dep.name}",
        f"  Containers:",
        f"   app:",
        f"    Image:   app:latest",
        f"    Port:    8080/TCP",
    ]
    for pod_name in dep.pods:
        pod = state.pods.get(pod_name)
        if pod:
            lines += [
                f"    Limits:",
                f"      memory: {pod.memory_limit}",
                f"    Usage:",
                f"      memory: {pod.memory_usage}",
            ]
            break
    lines += [
        f"Conditions:",
        f"  Available  {'True' if dep.ready > 0 else 'False'}",
        f"  Progressing True",
        f"Events:",
    ]
    if dep.ready < dep.replicas:
        lines.append(
            f"  Warning  Unavailable  deployment/{dep.name} does not have minimum "
            f"availability."
        )
    lines.append(f"  Pods: {pod_names}")
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
