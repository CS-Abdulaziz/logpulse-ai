"""
risk_assessor.py — Standalone risk assessment agent for LogPulse.

Architecture
------------
Scans the commands proposed by solution_agent_node against two ordered
pattern lists:

  1. FATAL_PATTERNS   — irreversible, destructive operations.
  2. WARNING_PATTERNS — impactful but potentially reversible operations.

Precedence: FATAL beats WARNING.  A single FATAL match elevates the
entire assessment to FATAL regardless of how many WARNING matches exist.

Risk level is INFORMATIONAL ONLY — there is no "blocked" flag.
The human operator always retains final execution authority.

Output
------
assess_commands() → RiskAssessment (see state.py for the full schema)

risk_assessor_node() → LangGraph-compatible node function
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap — importable from any working directory
# ---------------------------------------------------------------------------
_AGENTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_CACHE_DIR    = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "cache"))
_EVENTS_DIR   = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "events"))

for _p in (_WORKFLOW_DIR, _CACHE_DIR, _EVENTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import LogState, RiskAssessment, RiskLevel  # noqa: E402
from recorder import record   # noqa: E402
from models import EventType  # noqa: E402


# ---------------------------------------------------------------------------
# Pattern lists
# ---------------------------------------------------------------------------

# FATAL: irreversible, destructive operations.
# Any match → final_risk_level = FATAL
FATAL_PATTERNS: List[str] = [
    r"rm\s+-[rf]+\s*/",          # rm -rf / (recursive force delete from root)
    r"DROP\s+(DATABASE|TABLE)",   # SQL destructive DDL
    r"mkfs",                       # format a filesystem
    r"dd\s+if=",                   # raw disk write (dd if=...)
    r"shutdown\s+-h",              # system shutdown
    r">\s*/dev/sd[a-z]",           # redirect to block device
    r"truncate\s+-s\s+0\s+/",      # zero out a file under /
]

# WARNING: impactful but potentially reversible operations.
# Any match (no FATAL present) → final_risk_level = WARNING
WARNING_PATTERNS: List[str] = [
    r"systemctl\s+(stop|restart)",      # service stop/restart
    r"kubectl\s+(delete|drain|cordon)", # destructive k8s operations
    r"service\s+\w+\s+stop",           # legacy service stop
    r"ALTER\s+TABLE",                   # schema modification
    r"UPDATE\s+.+WHERE",               # bulk SQL UPDATE
    r"kubectl\s+rollout\s+restart",    # deployment restart
]

# Pre-compiled patterns with their string originals (kept together for reports)
_FATAL_COMPILED: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE | re.DOTALL), p) for p in FATAL_PATTERNS
]
_WARNING_COMPILED: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE | re.DOTALL), p) for p in WARNING_PATTERNS
]


# ---------------------------------------------------------------------------
# Core assessment function
# ---------------------------------------------------------------------------

def assess_commands(commands: List[str]) -> RiskAssessment:
    """
    Scan *commands* against FATAL and WARNING patterns and return a
    fully-populated ``RiskAssessment``.

    Algorithm
    ---------
    For each command:
      1. Check every FATAL pattern — if any hits, mark the command as
         flagged and record the pattern.
      2. If no FATAL hit, check every WARNING pattern — same logic.
      3. A command that matches neither is safe.

    Precedence:
      FATAL (any) > WARNING (any) > SAFE

    The returned ``RiskAssessment`` contains:
      • matched_patterns  — deduplicated list of matching regex strings
      • flagged_commands  — commands that triggered at least one match
      • safe_commands     — commands that cleared all patterns
      • requires_approval_count == len(flagged_commands)
      • auto_executable_count   == len(safe_commands)

    No command is *blocked* — risk level is informational only.
    """
    flagged: List[str]  = []
    safe:    List[str]  = []
    matched: List[str]  = []   # deduplicated pattern strings

    has_fatal   = False
    has_warning = False

    for cmd in commands:
        cmd_flagged  = False
        fatal_hit    = False

        # ── FATAL scan ───────────────────────────────────────────────────────
        for compiled, raw_pattern in _FATAL_COMPILED:
            if compiled.search(cmd):
                cmd_flagged = True
                fatal_hit   = True
                has_fatal   = True
                if raw_pattern not in matched:
                    matched.append(raw_pattern)

        # ── WARNING scan (only if no FATAL hit for this command) ─────────────
        if not fatal_hit:
            for compiled, raw_pattern in _WARNING_COMPILED:
                if compiled.search(cmd):
                    cmd_flagged = True
                    has_warning = True
                    if raw_pattern not in matched:
                        matched.append(raw_pattern)

        if cmd_flagged:
            flagged.append(cmd)
        else:
            safe.append(cmd)

    # ── Overall risk level ───────────────────────────────────────────────────
    if has_fatal:
        level   = RiskLevel.FATAL
        message = _build_message_fatal(flagged, matched, len(safe))
    elif has_warning:
        level   = RiskLevel.WARNING
        message = _build_message_warning(flagged, len(safe))
    else:
        level   = RiskLevel.SAFE
        message = _build_message_safe(len(safe))

    return RiskAssessment(
        final_risk_level        = level,
        matched_patterns        = matched,
        flagged_commands        = flagged,
        safe_commands           = safe,
        message                 = message,
        requires_approval_count = len(flagged),
        auto_executable_count   = len(safe),
    )


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_message_safe(n_safe: int) -> str:
    return (
        f"✅ RISK ASSESSMENT: SAFE\n"
        f"All {n_safe} command(s) cleared for execution."
    )


def _build_message_warning(flagged: List[str], n_safe: int) -> str:
    lines = [
        "⚠️  RISK ASSESSMENT: WARNING",
        f"Flagged : {flagged}",
        "Reason  : Impactful but reversible operation",
        f"Auto-executable : {n_safe} | Requires approval: {len(flagged)}",
    ]
    return "\n".join(lines)


def _build_message_fatal(
    flagged: List[str],
    patterns: List[str],
    n_safe: int,
) -> str:
    lines = [
        "🚨 RISK ASSESSMENT: FATAL",
        f"Command : {flagged}",
        f"Pattern : {patterns}",
        "THIS ACTION IS IRREVERSIBLE — proceed with caution.",
        f"Auto-executable : {n_safe} | Requires approval: {len(flagged)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LangGraph node function
# ---------------------------------------------------------------------------

def risk_assessor_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — risk-assess the commands proposed by solution_agent_node.

    Reads:  state.solution_result.commands  (preferred)
            state.solution.steps            (legacy fallback)

    Writes: state.security_check (RiskAssessment)

    The node NEVER raises.  Risk level is informational — the graph always
    continues; execution authority stays with the human operator.
    """
    commands: List[str] = []

    # Prefer the new SolutionResult.commands list
    if state.solution_result and state.solution_result.commands:
        commands = state.solution_result.commands
    # Legacy backward-compat: SolutionAnalysis.steps (List[CommandStep])
    elif state.solution and state.solution.steps:
        commands = [step.command for step in state.solution.steps]

    assessment = assess_commands(commands)

    # Print the assessment message to the console (visible in logs)
    print(f"\n[RiskAssessor] {assessment.message}\n")

    record(
        incident_id=state.trace_id,
        event_type=EventType.RISK_ASSESSED,
        node_name="risk_assessor_node",
        message=f"{assessment.final_risk_level.value} — {assessment.requires_approval_count} flagged",
        metadata={
            "level": assessment.final_risk_level.value,
            "flagged": len(assessment.flagged_commands),
            "safe": len(assessment.safe_commands),
        },
    )

    return {"security_check": assessment}
