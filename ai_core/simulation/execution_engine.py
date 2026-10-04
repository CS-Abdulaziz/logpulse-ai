"""
ai_core/simulation/execution_engine.py -- Execution Intelligence Layer.

Public surface
--------------
simulate_command(command, state=None) -> ExecutionResult
    Drop-in replacement for execute_in_sandbox() when a ClusterState is
    available.  Parses the command, dispatches to a handler, and returns a
    rich ExecutionResult with state_mutations tracking.

ExecutionEngine(state)
    Stateful engine instance.  Re-use across a session to accumulate mutations.

ExecutionResult
    Structured result: success, exit_code, stdout, stderr, state_mutations.
    Constructors: ok() / error() / noop().

Handler coverage
----------------
(verb, resource)       Behaviour
─────────────────────  ────────────────────────────────────────────────────────
get  pod               Pod table; single-pod detail if target given
get  deployment        Deployment table
get  node              Node table with pressure flags
get  all               Pods + deployments combined
get  events            Event table from ClusterState.events
get  service           Service table derived from deployments
describe pod           Full pod detail (status, memory, events)
describe deployment    Full deployment detail (replicas, limits, revision)
logs (none)            Log lines based on pod.status; honours --tail, --previous
top  pod               CPU / memory metrics table
top  node              Node resource usage table
set  resources         MUTATES memory_limit, cpu_limit, revision; updates pods
rollout restart        MUTATES pod statuses -> Running, increments revision
rollout status         Rollout progress based on available_replicas
scale (any resource)   MUTATES replicas count
patch (any resource)   MUTATES memory_limit from JSON patch payload
delete pod             MUTATES pod.status -> Pending (simulates replacement)
apply (none)           Acknowledged; no manifest state in simulator
exec (none)            Simulated exec output

Fallback
--------
Any unregistered (verb, resource) pair returns ExecutionResult.noop() at
exit_code=0, never exit -1.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_core.simulation.cluster_state import (
    ClusterEvent,
    ClusterState,
    DeploymentState,
    NodeState,
    PodState,
    seed_default_state,
)
from ai_core.simulation.intent_parser import KubectlIntent, parse_kubectl


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    success:          bool
    exit_code:        int
    stdout:           str
    stderr:           str
    state_mutations:  List[str]
    intent:           KubectlIntent

    # -- factories ----------------------------------------------------------

    @classmethod
    def ok(
        cls,
        intent: KubectlIntent,
        stdout: str,
        mutations: Optional[List[str]] = None,
    ) -> "ExecutionResult":
        return cls(
            success=True, exit_code=0,
            stdout=stdout, stderr="",
            state_mutations=mutations or [],
            intent=intent,
        )

    @classmethod
    def error(
        cls,
        intent: KubectlIntent,
        stderr: str,
        exit_code: int = 1,
    ) -> "ExecutionResult":
        return cls(
            success=False, exit_code=exit_code,
            stdout="", stderr=stderr,
            state_mutations=[],
            intent=intent,
        )

    @classmethod
    def noop(
        cls,
        intent: KubectlIntent,
        reason: str = "unsupported_intent",
    ) -> "ExecutionResult":
        verb = intent.verb
        res  = intent.resource or ""
        return cls(
            success=True, exit_code=0,
            stdout=f"[simulation] no-op -- {verb} {res}: {reason}",
            stderr="",
            state_mutations=[],
            intent=intent,
        )


# ---------------------------------------------------------------------------
# Handler type alias
# ---------------------------------------------------------------------------

_Handler = Callable[[KubectlIntent, ClusterState], ExecutionResult]


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """
    Dispatch kubectl intents to stateful handlers.

    The handler table maps (verb, resource) -> handler function.
    If no exact match exists, (verb, None) is tried as a wildcard.
    If neither matches, ExecutionResult.noop() is returned.
    """

    def __init__(self, state: ClusterState) -> None:
        self.state    = state
        self._table: Dict[Tuple[str, Optional[str]], _Handler] = _build_handler_table()

    def execute(self, intent: KubectlIntent) -> ExecutionResult:
        key     = (intent.verb, intent.resource)
        handler = self._table.get(key) or self._table.get((intent.verb, None))
        if handler is None:
            return ExecutionResult.noop(
                intent,
                reason=f"no handler for '{intent.verb} {intent.resource or ''}'"
            )
        return handler(intent, self.state)


# ---------------------------------------------------------------------------
# Public convenience function
# ---------------------------------------------------------------------------

def simulate_command(
    command: str,
    state: Optional[ClusterState] = None,
) -> ExecutionResult:
    """
    Drop-in replacement for the old regex-based sandbox.

    Parses *command*, dispatches via ExecutionEngine, returns ExecutionResult.
    If *state* is None a fresh default cluster is created (not cached).
    """
    if state is None:
        state = seed_default_state()
    intent = parse_kubectl(command)
    return ExecutionEngine(state).execute(intent)


# ---------------------------------------------------------------------------
# Handler table builder
# ---------------------------------------------------------------------------

def _build_handler_table() -> Dict[Tuple[str, Optional[str]], _Handler]:
    return {
        ("get",      "pod"):        _handle_get_pod,
        ("get",      "deployment"): _handle_get_deployment,
        ("get",      "node"):       _handle_get_node,
        ("get",      "all"):        _handle_get_all,
        ("get",      "events"):     _handle_get_events,
        ("get",      "service"):    _handle_get_service,
        ("describe", "pod"):        _handle_describe_pod,
        ("describe", "deployment"): _handle_describe_deployment,
        ("logs",     None):         _handle_logs,
        ("top",      "pod"):        _handle_top_pod,
        ("top",      "node"):       _handle_top_node,
        ("set",      "resources"):  _handle_set_resources,
        ("rollout",  "restart"):    _handle_rollout_restart,
        ("rollout",  "status"):     _handle_rollout_status,
        ("scale",    None):         _handle_scale,
        ("scale",    "deployment"): _handle_scale,
        ("scale",    "deploy"):     _handle_scale,
        ("patch",    None):         _handle_patch,
        ("patch",    "deployment"): _handle_patch,
        ("patch",    "deploy"):     _handle_patch,
        ("delete",   "pod"):        _handle_delete_pod,
        ("apply",    None):         _handle_apply,
        ("exec",     None):         _handle_exec,
    }


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

def _handle_get_pod(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns     = intent.namespace
    target = intent.target
    if target:
        pod = state.pods.get(target)
        if pod is None:
            return ExecutionResult.error(
                intent, f'Error from server (NotFound): pods "{target}" not found'
            )
        return ExecutionResult.ok(intent, _fmt_pod_detail(pod, state))
    pods = [p for p in state.pods.values() if p.namespace == ns]
    return ExecutionResult.ok(intent, _fmt_pod_table(pods, ns))


def _handle_get_deployment(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns   = intent.namespace
    deps = [d for d in state.deployments.values() if d.namespace == ns]
    return ExecutionResult.ok(intent, _fmt_deployment_table(deps, ns))


def _handle_get_node(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    nodes = list(state.nodes.values())
    if intent.target:
        nodes = [n for n in nodes if n.name == intent.target]
    return ExecutionResult.ok(intent, _fmt_node_table(nodes))


def _handle_get_all(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns   = intent.namespace
    pods = [p for p in state.pods.values() if p.namespace == ns]
    deps = [d for d in state.deployments.values() if d.namespace == ns]
    return ExecutionResult.ok(
        intent, f"{_fmt_pod_table(pods, ns)}\n\n{_fmt_deployment_table(deps, ns)}"
    )


def _handle_get_events(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns     = intent.namespace
    events = state.events or []
    if not events:
        return ExecutionResult.ok(intent, f"No events found in {ns} namespace.")
    header = f"{'LAST SEEN':<12}{'TYPE':<10}{'REASON':<22}{'OBJECT':<36}MESSAGE"
    lines  = [header]
    for ev in events:
        obj  = f"{ev.object_kind.lower()}/{ev.object_name}"
        line = f"{ev.timestamp:<12}{ev.event_type:<10}{ev.reason:<22}{obj:<36}{ev.message}"
        lines.append(line)
    return ExecutionResult.ok(intent, "\n".join(lines))


def _handle_get_service(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns   = intent.namespace
    deps = [d for d in state.deployments.values() if d.namespace == ns]
    if not deps:
        return ExecutionResult.ok(intent, f"No resources found in {ns} namespace.")
    header = f"{'NAME':<17}{'TYPE':<12}{'CLUSTER-IP':<16}{'EXTERNAL-IP':<14}{'PORT(S)':<12}AGE"
    lines  = [header]
    for i, dep in enumerate(deps):
        ip = f"10.96.{10 + i}.{20 + i}"
        lines.append(f"{dep.name:<17}ClusterIP   {ip:<16}<none>        8080/TCP    2d")
    return ExecutionResult.ok(intent, "\n".join(lines))


def _handle_describe_pod(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    pod    = state.pods.get(target)
    if pod is None:
        return ExecutionResult.error(
            intent, f'Error from server (NotFound): pods "{target}" not found'
        )
    return ExecutionResult.ok(intent, _fmt_pod_detail(pod, state))


def _handle_describe_deployment(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    dep    = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )
    return ExecutionResult.ok(intent, _fmt_deployment_detail(dep, state))


def _handle_logs(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target   = intent.target or ""
    pod      = state.pods.get(target)
    if pod is None:
        return ExecutionResult.error(
            intent, f'Error from server (NotFound): pods "{target}" not found'
        )
    tail     = intent.flags.get("tail")
    previous = bool(intent.flags.get("previous", False))
    lines    = _generate_logs(pod, previous=previous)
    if tail is not None:
        try:
            lines = lines[-int(tail):]
        except (ValueError, TypeError):
            pass
    return ExecutionResult.ok(intent, "\n".join(lines))


def _handle_top_pod(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    ns     = intent.namespace
    target = intent.target
    pods   = [p for p in state.pods.values() if p.namespace == ns]
    if target:
        pods = [p for p in pods if p.name == target]
    if not pods:
        return ExecutionResult.ok(intent, f"No resources found in {ns} namespace.")
    header = f"{'NAME':<32}{'CPU(cores)':<13}MEMORY(bytes)"
    lines  = [header]
    for pod in pods:
        cpu_str = f"{pod.cpu_millicores}m"
        mem_str = f"{pod.memory_mb}Mi"
        lines.append(f"{pod.name:<32}{cpu_str:<13}{mem_str}")
    return ExecutionResult.ok(intent, "\n".join(lines))


def _handle_top_node(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    nodes  = list(state.nodes.values())
    target = intent.target
    if target:
        nodes = [n for n in nodes if n.name == target]
    if not nodes:
        return ExecutionResult.ok(intent, "No nodes found.")
    header = f"{'NAME':<18}{'CPU(cores)':<13}{'CPU%':<7}{'MEMORY(bytes)':<16}MEMORY%"
    lines  = [header]
    for node in nodes:
        cpu_used   = int(node.allocatable_cpu_millicores * 0.30)
        cpu_pct    = f"{int(cpu_used / node.allocatable_cpu_millicores * 100)}%"
        mem_used   = int(node.allocatable_memory_mb * 0.55)
        mem_pct    = f"{int(mem_used / node.allocatable_memory_mb * 100)}%"
        flags_str  = " [MemoryPressure]" if node.memory_pressure else ""
        lines.append(
            f"{node.name + flags_str:<18}{cpu_used}m{'':<8}{cpu_pct:<7}"
            f"{mem_used}Mi{'':<9}{mem_pct}"
        )
    return ExecutionResult.ok(intent, "\n".join(lines))


def _handle_set_resources(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    dep    = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )

    limits_str = str(intent.flags.get("limits", ""))
    mem_m = re.search(r"memory=(\S+)", limits_str) or re.search(
        r"memory=(\S+)", intent.original_command
    )
    cpu_m = re.search(r"cpu=(\S+)", limits_str) or re.search(
        r"cpu=(\S+)", intent.original_command
    )

    if not mem_m and not cpu_m:
        return ExecutionResult.error(intent, "error: --limits flag required (memory= or cpu=)")

    mutations: List[str] = []
    old_rev = dep.revision

    if mem_m:
        new_mem = mem_m.group(1)
        old_mem = dep.memory_limit
        dep.memory_limit = new_mem
        new_mb = _parse_mb(new_mem)
        for pod in state.pods_for_deployment(target):
            pod.memory_limit_mb = new_mb
        mutations.append(f"{target}: memory_limit {old_mem} -> {new_mem}")

    if cpu_m:
        new_cpu = cpu_m.group(1)
        old_cpu = dep.cpu_limit
        dep.cpu_limit = new_cpu
        mutations.append(f"{target}: cpu_limit {old_cpu} -> {new_cpu}")

    dep.revision += 1
    mutations.append(f"{target}: revision {old_rev} -> {dep.revision}")

    state.log_event(ClusterEvent(
        timestamp="0s", event_type="Normal",
        reason="ScalingReplicaSet", object_kind="Deployment", object_name=target,
        message=f"Scaled replica set {target} with new resource limits",
    ))

    return ExecutionResult.ok(
        intent,
        stdout=f"deployment.apps/{target} resource requirements updated",
        mutations=mutations,
    )


def _handle_rollout_restart(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    dep    = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )

    mutations: List[str] = []
    old_rev = dep.revision
    dep.revision += 1
    mutations.append(f"{target}: revision {old_rev} -> {dep.revision}")

    for pod in state.pods_for_deployment(target):
        old_status = pod.status
        pod.status   = "Running"
        pod.restarts += 1
        if old_status == "OOMKilled":
            pod.memory_mb = max(0, pod.memory_limit_mb // 4)
        mutations.append(f"{pod.name}: {old_status} -> Running (restart #{pod.restarts})")

    dep.available_replicas = dep.replicas

    state.log_event(ClusterEvent(
        timestamp="0s", event_type="Normal",
        reason="RollingUpdate", object_kind="Deployment", object_name=target,
        message=f"Deployment {target} restarted",
    ))

    return ExecutionResult.ok(
        intent,
        stdout=f"deployment.apps/{target} restarted",
        mutations=mutations,
    )


def _handle_rollout_status(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    dep    = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )
    if dep.available_replicas >= dep.replicas:
        return ExecutionResult.ok(
            intent, f'deployment "{target}" successfully rolled out'
        )
    return ExecutionResult.ok(
        intent,
        f'Waiting for deployment "{target}" rollout to finish: '
        f'{dep.available_replicas} of {dep.replicas} updated replicas are available...',
    )


def _handle_scale(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target   = intent.target or ""
    replicas = intent.flags.get("replicas")
    if replicas is None:
        return ExecutionResult.error(intent, "error: --replicas flag required")

    dep = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )

    old_replicas           = dep.replicas
    dep.replicas           = int(replicas)
    dep.available_replicas = min(dep.available_replicas, dep.replicas)

    state.log_event(ClusterEvent(
        timestamp="0s", event_type="Normal",
        reason="ScalingReplicaSet", object_kind="Deployment", object_name=target,
        message=f"Scaled deployment/{target} from {old_replicas} to {dep.replicas}",
    ))

    return ExecutionResult.ok(
        intent,
        stdout=f"deployment.apps/{target} scaled",
        mutations=[f"{target}: replicas {old_replicas} -> {dep.replicas}"],
    )


def _handle_patch(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    dep    = state.deployments.get(target)
    if dep is None:
        return ExecutionResult.error(
            intent,
            f'Error from server (NotFound): deployments.apps "{target}" not found',
        )

    mem_m = re.search(r'"memory"\s*:\s*"([^"]+)"', intent.original_command)
    mutations: List[str] = []

    if mem_m:
        new_mem = mem_m.group(1)
        old_mem = dep.memory_limit
        dep.memory_limit = new_mem
        dep.revision    += 1
        new_mb = _parse_mb(new_mem)
        for pod in state.pods_for_deployment(target):
            pod.memory_limit_mb = new_mb
        mutations.append(f"{target}: memory_limit {old_mem} -> {new_mem}")
        mutations.append(f"{target}: revision incremented to {dep.revision}")

    return ExecutionResult.ok(
        intent,
        stdout=f"deployment.apps/{target} patched",
        mutations=mutations,
    )


def _handle_delete_pod(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    ns     = intent.namespace
    pod    = state.pods.get(target)
    if pod is None or pod.namespace != ns:
        return ExecutionResult.error(
            intent, f'Error from server (NotFound): pods "{target}" not found'
        )

    old_status   = pod.status
    pod.status   = "Pending"   # simulate replacement starting
    pod.restarts += 1

    # decrement deployment availability
    for dep in state.deployments.values():
        if target in dep.pods:
            dep.available_replicas = max(0, dep.available_replicas - 1)
            break

    state.log_event(ClusterEvent(
        timestamp="0s", event_type="Normal",
        reason="Killing", object_kind="Pod", object_name=target,
        message=f"Pod {target} deleted; replacement pending",
    ))

    return ExecutionResult.ok(
        intent,
        stdout=f'pod "{target}" deleted',
        mutations=[f"{target}: {old_status} -> Pending (replacement scheduled)"],
    )


def _handle_apply(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    file_hint = str(intent.flags.get("filename", intent.flags.get("f", "<manifest>")))
    return ExecutionResult.ok(
        intent,
        stdout=f"[simulation] applied {file_hint} (no manifest state in simulator)",
    )


def _handle_exec(intent: KubectlIntent, state: ClusterState) -> ExecutionResult:
    target = intent.target or ""
    pod    = state.pods.get(target)
    if pod is None:
        return ExecutionResult.error(
            intent, f'Error from server (NotFound): pods "{target}" not found'
        )
    if pod.status != "Running":
        return ExecutionResult.error(
            intent,
            f"error: unable to upgrade connection: container not running ({pod.status})",
        )
    exec_m   = re.search(r"--\s+(.+)$", intent.original_command)
    exec_cmd = exec_m.group(1).strip() if exec_m else "sh"
    return ExecutionResult.ok(
        intent, stdout=f"[simulation] exec in {target}: {exec_cmd}"
    )


# ---------------------------------------------------------------------------
# Output formatters (private)
# ---------------------------------------------------------------------------

def _fmt_pod_table(pods: List[PodState], ns: str) -> str:
    if not pods:
        return f"No resources found in {ns} namespace."
    header = f"{'NAME':<32}{'READY':<8}{'STATUS':<20}{'RESTARTS':<11}AGE"
    lines  = [header]
    for pod in pods:
        age    = _fmt_age(pod.age_seconds)
        status = pod.status
        lines.append(f"{pod.name:<32}{'1/1':<8}{status:<20}{pod.restarts:<11}{age}")
    return "\n".join(lines)


def _fmt_deployment_table(deps: List[DeploymentState], ns: str) -> str:
    if not deps:
        return f"No resources found in {ns} namespace."
    header = f"{'NAME':<17}{'READY':<8}{'UP-TO-DATE':<13}{'AVAILABLE':<12}AGE"
    lines  = [header]
    for dep in deps:
        ready = f"{dep.available_replicas}/{dep.replicas}"
        lines.append(f"{dep.name:<17}{ready:<8}{dep.replicas:<13}{dep.available_replicas:<12}2d")
    return "\n".join(lines)


def _fmt_node_table(nodes: List[NodeState]) -> str:
    if not nodes:
        return "No nodes found."
    header = f"{'NAME':<18}{'STATUS':<12}{'ROLES':<9}{'AGE':<7}VERSION"
    lines  = [header]
    for node in nodes:
        status = node.status
        if node.memory_pressure:
            status += ",MemoryPressure"
        lines.append(f"{node.name:<18}{status:<12}{'worker':<9}{'10d':<7}v1.28.0")
    return "\n".join(lines)


def _fmt_pod_detail(pod: PodState, state: ClusterState) -> str:
    exit_code = "137" if pod.status == "OOMKilled" else "0"
    pod_events = [e for e in state.events if e.object_name == pod.name]
    lines = [
        f"Name:         {pod.name}",
        f"Namespace:    {pod.namespace}",
        f"Node:         {pod.node}",
        f"Status:       {pod.status}",
        f"Containers:",
        f"  app:",
        f"    Image:    app:latest",
        f"    Limits:",
        f"      memory: {pod.memory_limit_mb}Mi",
        f"      cpu:    {pod.cpu_limit_millicores}m",
        f"    Usage:",
        f"      memory: {pod.memory_mb}Mi",
        f"      cpu:    {pod.cpu_millicores}m",
        f"    Last State:",
        f"      Reason:    {pod.status}",
        f"      Exit Code: {exit_code}",
        f"    Restart Count: {pod.restarts}",
        f"Events:",
    ]
    for ev in pod_events:
        lines.append(f"  {ev.event_type:<8}  {ev.reason:<18}  {ev.message}")
    if not pod_events and pod.status == "OOMKilled":
        lines.append(
            "  Warning   OOMKilling          Container app exceeded memory limit"
        )
    return "\n".join(lines)


def _fmt_deployment_detail(dep: DeploymentState, state: ClusterState) -> str:
    pod_names = ", ".join(dep.pods) or "<none>"
    lines = [
        f"Name:                   {dep.name}",
        f"Namespace:              {dep.namespace}",
        f"Selector:               app={dep.name}",
        f"Replicas:               {dep.replicas} desired | {dep.available_replicas} available",
        f"StrategyType:           RollingUpdate",
        f"Revision:               {dep.revision}",
        f"Image:                  {dep.image}",
        f"Limits:",
        f"  memory: {dep.memory_limit}",
        f"  cpu:    {dep.cpu_limit}",
        f"Conditions:",
        f"  Available  {'True' if dep.available_replicas > 0 else 'False'}",
        f"  Progressing True",
        f"Pods: {pod_names}",
    ]
    return "\n".join(lines)


def _generate_logs(pod: PodState, previous: bool = False) -> List[str]:
    ts = "2025-01-15T22"
    if pod.status == "OOMKilled" or previous:
        return [
            f"{ts}:10:00Z WARN  Memory usage rising: 78% of {pod.memory_limit_mb}Mi",
            f"{ts}:12:30Z WARN  Memory critical: 91% of {pod.memory_limit_mb}Mi",
            f"{ts}:13:55Z ERROR Memory critical: 94% of {pod.memory_limit_mb}Mi -- GC thrashing",
            f"{ts}:14:07Z FATAL OOMKilled: container exceeded {pod.memory_limit_mb}Mi limit",
        ]
    if pod.status == "CrashLoopBackOff":
        return [
            f"{ts}:14:00Z INFO  Container starting",
            f"{ts}:14:01Z ERROR Uncaught exception in main goroutine: nil pointer deref",
            f"{ts}:14:01Z FATAL Process exited with code 2",
        ]
    return [
        f"{ts}:14:07Z INFO  {pod.name} started successfully",
        f"{ts}:14:08Z INFO  Listening on :8080",
        f"{ts}:14:09Z INFO  Database connection pool initialised (32 conns)",
        f"{ts}:14:10Z INFO  Health check passed",
    ]


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _parse_mb(value: str) -> int:
    """Convert '512Mi', '1Gi', '256M', '1G' -> integer MB."""
    v = value.strip().upper()
    try:
        if v.endswith("GI"):
            return int(v[:-2]) * 1024
        if v.endswith("MI"):
            return int(v[:-2])
        if v.endswith("G"):
            return int(v[:-1]) * 1000
        if v.endswith("M"):
            return int(v[:-1])
        return int(v) // (1024 * 1024)
    except ValueError:
        return 256


def _fmt_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
