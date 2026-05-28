"""
ai_core/simulation/models.py — Pydantic models for the in-memory cluster simulation.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class PodStatus(str, Enum):
    RUNNING   = "Running"
    OOMKILLED = "OOMKilled"
    PENDING   = "Pending"
    CRASHED   = "CrashLoopBackOff"
    COMPLETED = "Completed"
    UNKNOWN   = "Unknown"


class DeploymentStatus(str, Enum):
    AVAILABLE    = "Available"
    PROGRESSING  = "Progressing"
    DEGRADED     = "Degraded"


class SimulatedPod(BaseModel):
    name:          str
    namespace:     str              = "production"
    status:        PodStatus        = PodStatus.RUNNING
    restarts:      int              = 0
    memory_usage:  str              = "0Mi"
    memory_limit:  str              = "256Mi"
    node:          str              = "worker-1"
    age:           str              = "2d"
    ready:         str              = "1/1"


class SimulatedDeployment(BaseModel):
    name:          str
    namespace:     str              = "production"
    status:        DeploymentStatus = DeploymentStatus.AVAILABLE
    replicas:      int              = 1
    ready:         int              = 1
    pods:          List[str]        = Field(default_factory=list)


class SimulatedNode(BaseModel):
    name:          str
    status:        str              = "Ready"
    roles:         str              = "worker"
    age:           str              = "10d"
    version:       str              = "v1.28.0"
    temperature:   Optional[float] = None
    hardware_faults: int            = 0
    online:        bool             = True


class SimulatedSSHState(BaseModel):
    failed_attempts:    int              = 0
    attacker_ip:        str              = ""
    locked_accounts:    List[str]        = Field(default_factory=list)
    blocked_ips:        List[str]        = Field(default_factory=list)
    active_connections: int              = 0


class SimulatedHDFSBlock(BaseModel):
    block_id:   str
    datanode:   str
    corrupt:    bool  = False
    replicas:   int   = 3


class SimulatedHDFSState(BaseModel):
    total_blocks:    int                       = 48291
    datanodes:       List[str]                 = Field(default_factory=list)
    corrupt_blocks:  List[SimulatedHDFSBlock]  = Field(default_factory=list)
    missing_blocks:  int                       = 0
    healthy:         bool                      = True


class SimulatedBlueGeneState(BaseModel):
    nodes:          List[SimulatedNode] = Field(default_factory=list)
    jobs_killed:    int                 = 0
    jobs_rescheduled: int               = 0


class ClusterState(BaseModel):
    """Top-level simulation state — shared across all commands in a session."""
    scenario:    str                              = ""
    pods:        Dict[str, SimulatedPod]          = Field(default_factory=dict)
    deployments: Dict[str, SimulatedDeployment]   = Field(default_factory=dict)
    nodes:       Dict[str, SimulatedNode]         = Field(default_factory=dict)
    ssh:         Optional[SimulatedSSHState]      = None
    hdfs:        Optional[SimulatedHDFSState]     = None
    bluegene:    Optional[SimulatedBlueGeneState] = None
    free_mem_mb: int                              = 512
