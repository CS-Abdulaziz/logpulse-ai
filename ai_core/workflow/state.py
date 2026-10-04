from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

class SeverityLevel(str, Enum):
    INFO = "Info"
    WARNING = "Warning"
    CRITICAL = "Critical"
    FATAL = "Fatal"

class RiskLevel(str, Enum):
    SAFE    = "safe"
    WARNING = "warning"
    MEDIUM  = "Medium"   # legacy – kept for backward compat
    HIGH    = "High"     # legacy – kept for backward compat
    FATAL   = "fatal"

class WorkflowStatus(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    WAITING_FOR_APPROVAL = "WaitingForApproval"
    COMPLETED = "Completed"
    FAILED = "Failed"

class ClassificationData(BaseModel):
    category: str = Field(..., min_length=2)
    source: str = Field(..., min_length=2)
    severity: SeverityLevel
    summary: str = Field(..., min_length=5)

class RoutingContext(BaseModel):
    route: Literal[
        "skip",
        "standard_flow",
        "fast_track"
    ] = "standard_flow"

    priority: Literal[
        "P1",
        "P2",
        "P3",
        "P4"
    ] = "P3"

class RagResult(BaseModel):
    playbook_steps: Optional[str] = None
    rag_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0
    )

class HistoryResult(BaseModel):
    historical_incidents: Optional[str] = None

class HistoryContext(BaseModel):
    """Context returned by history_node to downstream nodes."""
    is_first_occurrence: bool
    occurrence_count: int = 0
    previous_summary: Optional[str] = None
    previous_root_cause: Optional[str] = None
    previous_solution: Optional[str] = None
    previous_outcome: Optional[str] = None
    last_seen: Optional[datetime] = None

class MergedKnowledge(BaseModel):
    playbook_steps: Optional[str] = None
    rag_confidence: Optional[float] = None
    historical_incidents: Optional[str] = None

class DiagnosticAnalysis(BaseModel):
    root_cause: Optional[str] = None
    confidence_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0
    )

class DiagnosticResult(BaseModel):
    """Rich diagnostic output produced by diagnostic_agent_node."""
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    """Short explanation of which evidence drove the conclusion."""
    used_history: bool
    """True if prior incident history informed the diagnosis."""
    used_playbook: bool
    """True if the retrieved RAG playbook informed the diagnosis."""
    source: str
    """One of: 'llm' | 'playbook_fallback' | 'safe_fallback'"""

class CommandStep(BaseModel):
    command: str = Field(..., min_length=1)
    explanation: str = Field(..., min_length=3)
    requires_sudo: bool = False
    destructive: bool = False

class SolutionAnalysis(BaseModel):
    steps: List[CommandStep] = Field(default_factory=list)
    initial_risk_level: Optional[RiskLevel] = None

class SolutionResult(BaseModel):
    """Rich remediation plan produced by solution_agent_node."""
    steps: List[str] = Field(default_factory=list)
    """Human-readable action descriptions, one per remediation step."""
    commands: List[str] = Field(default_factory=list)
    """Concrete, executable CLI / Bash / Kubectl command strings."""
    explanation: str
    """One-paragraph summary of why these steps address the root cause."""
    source: str
    """One of: 'llm' | 'playbook_fallback' | 'safe_fallback'"""

class RiskAssessment(BaseModel):
    """Rich risk assessment produced by risk_assessor_node."""
    final_risk_level:        RiskLevel = RiskLevel.SAFE
    matched_patterns:        List[str] = Field(default_factory=list)
    """Regex pattern strings that triggered a match."""
    flagged_commands:        List[str] = Field(default_factory=list)
    """Commands that triggered a FATAL or WARNING pattern."""
    safe_commands:           List[str] = Field(default_factory=list)
    """Commands that cleared all patterns."""
    message:                 str = ""
    """Human-readable summary of the assessment outcome."""
    requires_approval_count: int = 0
    """Number of commands that require human approval before execution."""
    auto_executable_count:   int = 0
    """Number of commands cleared for automated execution."""

class OperatorDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"

class SandboxResult(BaseModel):
    """Result of running one command inside the Docker sandbox."""
    command:           str
    stdout:            str = ""
    stderr:            str = ""
    exit_code:         int = -1
    execution_time_ms: int = 0

class HumanReviewResult(BaseModel):
    """The full outcome of the human-in-the-loop review step."""
    decision:        OperatorDecision
    operator_note:   str = ""
    sandbox_results: List[SandboxResult] = Field(default_factory=list)
    executed_at:     str  # ISO-8601 timestamp string

class ExecutionMetadata(BaseModel):
    workflow_status: WorkflowStatus = WorkflowStatus.PENDING
    retry_count: int = Field(default=0, ge=0)
    processing_time_ms: Optional[int] = Field(default=None, ge=0)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completed_at: Optional[datetime] = None

class LogState(BaseModel):
    trace_id: str = Field(
        default_factory=lambda: str(uuid.uuid4())
    )

    raw_log: str = Field(
        ...,
        min_length=10
    )

    duplicate_count: int = Field(
        default=1,
        ge=1
    )

    classification: Optional[ClassificationData] = None
    routing: Optional[RoutingContext] = None
    user_approved: Optional[bool] = None

    rag_result: Optional[RagResult] = None
    history_result: Optional[HistoryResult] = None
    history_context: Optional[HistoryContext] = None
    merged_knowledge: Optional[MergedKnowledge] = None

    diagnostic: Optional[DiagnosticAnalysis] = None
    diagnostic_result: Optional[DiagnosticResult] = None
    solution: Optional[SolutionAnalysis] = None
    solution_result: Optional[SolutionResult] = None
    security_check: Optional[RiskAssessment] = None
    human_review: Optional[HumanReviewResult] = None

    cluster_state: Optional[Any] = None
    """In-memory ClusterState for simulation mode; None uses Docker."""

    execution: ExecutionMetadata = Field(
        default_factory=ExecutionMetadata
    )