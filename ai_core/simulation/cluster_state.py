"""
ai_core/simulation/cluster_state.py — Standalone in-memory Kubernetes cluster model.

Used exclusively by the ExecutionEngine layer.  The existing sim_models.py /
command_router.py pipeline remains unchanged and continues to serve
execute_in_sandbox().

Key design choices
------------------
- Pure stdlib dataclasses — no Pydantic, no external deps.
- Integer MB fields (memory_mb, memory_limit_mb) keep arithmetic simple and
  testable; human-readable string fields (memory_limit, cpu_limit) hold the
  canonical kubectl-style value ("512Mi", "1Gi", "500m").
- snapshot() / restore() give deterministic round-trip semantics; handlers
  can snapshot before a mutation and restore on error.
- ClusterEvent.log_event() records side-effects for test assertions.

Factories
---------
seed_oom_state()     OOM-critical scenario: worker-api OOMKilled, 1 node under
                     memory pressure.  Used by most execution-layer tests.
seed_default_state() Broader cluster: 3 deployments, 5 pods, 2 nodes.  Used
                     by the demo script and integration tests.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PodState:
    name:                  str
    namespace:             str   = "production"
    status:                str   = "Running"  # Running | OOMKilled | CrashLoopBackOff | Pending | Completed
    restarts:              int   = 0
    memory_mb:             int   = 0          # current RSS
    memory_limit_mb:       int   = 256        # container limit
    cpu_millicores:        int   = 0          # current CPU usage
    cpu_limit_millicores:  int   = 500
    node:                  str   = "worker-1"
    age_seconds:           int   = 86400      # 1 day


@dataclass
class DeploymentState:
    name:                str
    namespace:           str        = "production"
    replicas:            int        = 1
    available_replicas:  int        = 1
    memory_limit:        str        = "256Mi"  # kubectl-style string
    cpu_limit:           str        = "500m"
    image:               str        = "app:latest"
    revision:            int        = 1
    pods:                List[str]  = field(default_factory=list)


@dataclass
class NodeState:
    name:                      str
    status:                    str   = "Ready"
    memory_pressure:           bool  = False
    cpu_pressure:              bool  = False
    allocatable_memory_mb:     int   = 4096
    allocatable_cpu_millicores: int  = 4000


@dataclass
class ClusterEvent:
    timestamp:    str
    event_type:   str   # "Warning" | "Normal"
    reason:       str
    object_kind:  str   # "Pod" | "Deployment"
    object_name:  str
    message:      str


# ---------------------------------------------------------------------------
# ClusterState
# ---------------------------------------------------------------------------

class ClusterState:
    """In-memory simulation of a Kubernetes cluster."""

    def __init__(self) -> None:
        self.pods:        Dict[str, PodState]        = {}
        self.deployments: Dict[str, DeploymentState] = {}
        self.nodes:       Dict[str, NodeState]       = {}
        self.events:      List[ClusterEvent]         = []

    # ------------------------------------------------------------------
    # Snapshot / restore — deep-copy semantics for determinism tests
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a deep-copy of the entire cluster state."""
        return {
            "pods":        {k: copy.deepcopy(v) for k, v in self.pods.items()},
            "deployments": {k: copy.deepcopy(v) for k, v in self.deployments.items()},
            "nodes":       {k: copy.deepcopy(v) for k, v in self.nodes.items()},
            "events":      copy.deepcopy(self.events),
        }

    def restore(self, snap: dict) -> None:
        """Replace current state with a previously taken snapshot."""
        self.pods        = {k: copy.deepcopy(v) for k, v in snap["pods"].items()}
        self.deployments = {k: copy.deepcopy(v) for k, v in snap["deployments"].items()}
        self.nodes       = {k: copy.deepcopy(v) for k, v in snap["nodes"].items()}
        self.events      = copy.deepcopy(snap["events"])

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def log_event(self, event: ClusterEvent) -> None:
        self.events.append(event)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def pods_for_deployment(self, dep_name: str) -> List[PodState]:
        dep = self.deployments.get(dep_name)
        if dep is None:
            return []
        return [self.pods[p] for p in dep.pods if p in self.pods]

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ClusterState(pods={len(self.pods)}, "
            f"deployments={len(self.deployments)}, "
            f"nodes={len(self.nodes)}, events={len(self.events)})"
        )


# ---------------------------------------------------------------------------
# Factory: OOM-critical scenario
# ---------------------------------------------------------------------------

def seed_oom_state() -> ClusterState:
    """
    Return a ClusterState matching the oom_critical scenario:
    - Deployment worker-api: 1 replica, 0 available, memory_limit=256Mi, revision=7
    - Pod worker-api-7d9f8b-xk2q: OOMKilled, 14 restarts, 241 MB / 256 MB limit
    - Node worker-1: memory pressure active
    """
    state = ClusterState()

    state.pods["worker-api-7d9f8b-xk2q"] = PodState(
        name             = "worker-api-7d9f8b-xk2q",
        namespace        = "production",
        status           = "OOMKilled",
        restarts         = 14,
        memory_mb        = 241,
        memory_limit_mb  = 256,
        cpu_millicores   = 245,
        cpu_limit_millicores = 500,
        node             = "worker-1",
        age_seconds      = 7200,
    )

    state.deployments["worker-api"] = DeploymentState(
        name               = "worker-api",
        namespace          = "production",
        replicas           = 1,
        available_replicas = 0,
        memory_limit       = "256Mi",
        cpu_limit          = "500m",
        image              = "worker-api:v1.7",
        revision           = 7,
        pods               = ["worker-api-7d9f8b-xk2q"],
    )

    state.nodes["worker-1"] = NodeState(
        name                    = "worker-1",
        status                  = "Ready",
        memory_pressure         = True,
        cpu_pressure            = False,
        allocatable_memory_mb   = 4096,
        allocatable_cpu_millicores = 4000,
    )

    state.log_event(ClusterEvent(
        timestamp   = "2m",
        event_type  = "Warning",
        reason      = "OOMKilling",
        object_kind = "Pod",
        object_name = "worker-api-7d9f8b-xk2q",
        message     = "Memory limit reached -- container killed",
    ))

    return state


# ---------------------------------------------------------------------------
# Factory: broader default cluster
# ---------------------------------------------------------------------------

def seed_default_state() -> ClusterState:
    """
    Return a ClusterState with 3 deployments (worker-api OOM, payment-svc and
    cache-service healthy), 5 pods, and 2 nodes.
    """
    state = seed_oom_state()

    # payment-svc — healthy
    state.pods["payment-svc-5f4b9-zx1w"] = PodState(
        name="payment-svc-5f4b9-zx1w", namespace="production",
        status="Running", restarts=0,
        memory_mb=64, memory_limit_mb=512,
        cpu_millicores=120, cpu_limit_millicores=1000,
        node="worker-2", age_seconds=172800,
    )
    state.pods["payment-svc-5f4b9-ab2c"] = PodState(
        name="payment-svc-5f4b9-ab2c", namespace="production",
        status="Running", restarts=0,
        memory_mb=71, memory_limit_mb=512,
        cpu_millicores=95, cpu_limit_millicores=1000,
        node="worker-1", age_seconds=172800,
    )
    state.deployments["payment-svc"] = DeploymentState(
        name="payment-svc", namespace="production",
        replicas=2, available_replicas=2,
        memory_limit="512Mi", cpu_limit="1000m",
        image="payment-svc:v2.3", revision=3,
        pods=["payment-svc-5f4b9-zx1w", "payment-svc-5f4b9-ab2c"],
    )

    # cache-service — healthy
    state.pods["cache-service-9d1c2-kp7q"] = PodState(
        name="cache-service-9d1c2-kp7q", namespace="production",
        status="Running", restarts=1,
        memory_mb=128, memory_limit_mb=256,
        cpu_millicores=50, cpu_limit_millicores=500,
        node="worker-2", age_seconds=259200,
    )
    state.pods["cache-service-9d1c2-mn4r"] = PodState(
        name="cache-service-9d1c2-mn4r", namespace="production",
        status="Running", restarts=0,
        memory_mb=131, memory_limit_mb=256,
        cpu_millicores=48, cpu_limit_millicores=500,
        node="worker-1", age_seconds=259200,
    )
    state.deployments["cache-service"] = DeploymentState(
        name="cache-service", namespace="production",
        replicas=2, available_replicas=2,
        memory_limit="256Mi", cpu_limit="500m",
        image="cache-service:v1.1", revision=1,
        pods=["cache-service-9d1c2-kp7q", "cache-service-9d1c2-mn4r"],
    )

    # second node
    state.nodes["worker-2"] = NodeState(
        name="worker-2", status="Ready",
        memory_pressure=False, cpu_pressure=False,
        allocatable_memory_mb=8192,
        allocatable_cpu_millicores=8000,
    )

    return state
