"""
sandbox_executor.py — Isolated Docker sandbox for executing remediation commands.

Architecture
------------
Each command runs inside a fresh Docker container:

    docker run
        --rm
        --network=none
        --memory=128m
        --cpus=0.5
        --workdir=/sandbox
        logpulse-sandbox:latest
        sh -c "<command>"

Guarantees
----------
- Network isolation (--network=none)
- Memory cap (128 MB)
- CPU cap (0.5 core)
- 15-second timeout — container is killed on overrun
- Docker-unavailable fallback — returns a graceful SandboxResult instead of crashing

Env vars
--------
LOGPULSE_AUTO_APPROVE=true   → skip Docker, return a simulated success result
                               (required for CI / automated tests)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import List

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_AGENTS_DIR      = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR    = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_EVENTS_DIR      = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "events"))
_SIMULATION_DIR  = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "simulation"))

for _p in (_WORKFLOW_DIR, _EVENTS_DIR, _SIMULATION_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import SandboxResult          # noqa: E402
from recorder import record              # noqa: E402
from models import EventType             # noqa: E402
from command_router import route as _sim_route  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DOCKER_IMAGE   = "logpulse-sandbox:latest"
TIMEOUT_SECS   = 15
MEMORY_LIMIT   = "128m"
CPU_LIMIT      = "0.5"

_AUTO_APPROVE_ENV = "LOGPULSE_AUTO_APPROVE"


# ---------------------------------------------------------------------------
# Core executor
# ---------------------------------------------------------------------------

def execute_in_sandbox(
    command: str,
    incident_id: str = "",
    cluster_state=None,
) -> SandboxResult:
    """
    Run *command* inside a Docker sandbox container and return a SandboxResult.

    Auto-approve shortcut
    ---------------------
    If the ``LOGPULSE_AUTO_APPROVE`` env var is ``"true"`` (case-insensitive),
    Docker is never invoked.  A synthetic ``SandboxResult(exit_code=0)`` is
    returned so test suites can exercise the pipeline without Docker.

    Docker-unavailable fallback
    ---------------------------
    If Docker is not installed (``FileNotFoundError``), a SandboxResult with
    ``exit_code=-1`` and a human-readable stdout is returned.  The pipeline
    continues uninterrupted.

    Timeout
    -------
    If the container runs longer than ``TIMEOUT_SECS``, it is forcibly killed.
    ``stderr`` will contain ``"[timeout after 15s]"`` and ``exit_code=-1``.
    """
    # ── Simulation mode (cluster_state provided) ─────────────────────────────
    if cluster_state is not None:
        result = _execute_simulated(command, cluster_state)
        _print_result(result)
        _emit_event(result, incident_id)
        return result

    # ── Auto-approve shortcut (CI / tests) ───────────────────────────────────
    if os.environ.get(_AUTO_APPROVE_ENV, "").lower() == "true":
        result = SandboxResult(
            command=command,
            stdout=f"[auto-approve] simulated execution of: {command}",
            stderr="",
            exit_code=0,
            execution_time_ms=0,
        )
        _print_result(result)
        _emit_event(result, incident_id)
        return result

    docker_cmd = [
        "docker", "run",
        "--rm",
        "--network=none",
        f"--memory={MEMORY_LIMIT}",
        f"--cpus={CPU_LIMIT}",
        "--workdir=/sandbox",
        DOCKER_IMAGE,
        "sh", "-c", command,
    ]

    t_start = time.monotonic()
    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
        )
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        result = SandboxResult(
            command=command,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            exit_code=proc.returncode,
            execution_time_ms=elapsed_ms,
        )

    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        result = SandboxResult(
            command=command,
            stdout="",
            stderr=f"[timeout after {TIMEOUT_SECS}s]",
            exit_code=-1,
            execution_time_ms=elapsed_ms,
        )

    except FileNotFoundError:
        result = SandboxResult(
            command=command,
            stdout="[sandbox unavailable — docker not found]",
            stderr="",
            exit_code=-1,
            execution_time_ms=0,
        )

    _print_result(result)
    _emit_event(result, incident_id)
    return result


def execute_commands_in_sandbox(
    commands: List[str],
    incident_id: str = "",
    cluster_state=None,
) -> List[SandboxResult]:
    """Execute a list of commands sequentially and return all results."""
    results: List[SandboxResult] = []
    for cmd in commands:
        results.append(
            execute_in_sandbox(cmd, incident_id=incident_id, cluster_state=cluster_state)
        )
    return results


# ---------------------------------------------------------------------------
# Simulation executor
# ---------------------------------------------------------------------------

def _execute_simulated(command: str, cluster_state) -> SandboxResult:
    """Route *command* through the in-memory simulation instead of Docker."""
    t_start = time.monotonic()
    cmd_result = _sim_route(command, cluster_state)
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return SandboxResult(
        command=command,
        stdout=cmd_result.stdout,
        stderr=cmd_result.stderr,
        exit_code=cmd_result.exit_code,
        execution_time_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _print_result(result: SandboxResult) -> None:
    icon = "✓" if result.exit_code == 0 else "✗"
    print(
        f"  {icon} {result.command} → exit {result.exit_code} "
        f"({result.execution_time_ms}ms)"
    )


def _emit_event(result: SandboxResult, incident_id: str) -> None:
    if not incident_id:
        return
    event_type = (
        EventType.SANDBOX_EXECUTED if result.exit_code == 0
        else EventType.SANDBOX_FAILED
    )
    record(
        incident_id=incident_id,
        event_type=event_type,
        node_name="sandbox_executor",
        message=f"{result.command[:80]} (exit {result.exit_code})",
        metadata={"exit_code": result.exit_code, "ms": result.execution_time_ms},
    )
