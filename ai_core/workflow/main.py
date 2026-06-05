"""
ai_core/workflow/main.py — LogPulse AI entry point

Three input modes:
  --log    "raw log string"          single log, direct pipeline execution
  --file   path.log --from T --to T  log file with timestamp range filter
  --stream mock --scenario NAME      simulated real-time stream
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

_THIS_DIR = Path(os.path.abspath(__file__)).parent   # ai_core/workflow/

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from ai_core.workflow.graph import logpulse_app
from ai_core.cache.cache_node import get_cache_stats
from ai_core.workflow.agents.history_agent import get_history_manager

from ai_core.ingestion.filters import keyword_filter, dedup_filter
from ai_core.ingestion.log_queue import LogQueue
from ai_core.ingestion.file_reader import read_file_logs
from ai_core.ingestion.stream_reader import read_stream_mock

from ai_core.simulation.cluster_state import (
    ClusterState as ExecClusterState,
    seed_default_state,
    seed_oom_state,
)

_PROJECT_ROOT  = _THIS_DIR.parent.parent
_SCENARIOS_DIR = _PROJECT_ROOT / "data" / "scenarios"

# ---------------------------------------------------------------------------
# Scenario → ClusterState factory mapping
# Extend this dict as new scenario types are added.
# ---------------------------------------------------------------------------
_SCENARIO_CLUSTER_FACTORIES = {
    "oom_critical": seed_oom_state,
}

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_log_counter   = 0     # incremented per run_pipeline call
_cluster_state: ExecClusterState | None = None  # shared state for --stream mock runs


def run_pipeline(raw_log: str) -> None:
    """Run the full LogPulse workflow for a single raw log string."""
    global _log_counter
    _log_counter += 1

    print(f"\n{'=' * 38}")
    print(f"Processing log {_log_counter}")
    print(f"{'=' * 38}")

    # ── Execution cluster state ───────────────────────────────────────────────
    # For --stream mock: _cluster_state is the shared ExecClusterState instance
    # so mutations accumulate across stream events (realistic simulation).
    # For --log / --file: create a fresh default state per run (isolated).
    if isinstance(_cluster_state, ExecClusterState):
        run_cluster_state: ExecClusterState = _cluster_state
    else:
        run_cluster_state = seed_default_state()

    initial_state = {"raw_log": raw_log, "cluster_state": run_cluster_state}

    print(f"\n[Input] {raw_log[:120]}{'...' if len(raw_log) > 120 else ''}\n")

    full_result = logpulse_app.invoke(initial_state)

    # ── Classification ──────────────────────────────────────────────────────
    classification = full_result.get("classification")
    if classification:
        print(f"[LOGPULSE] category: {classification.category} "
              f"| severity: {classification.severity.value}")

    # ── Diagnostic ─────────────────────────────────────────────────────────
    diag = full_result.get("diagnostic_result")
    if diag:
        print(f"\n[Diagnosis]")
        print(f"  Root Cause : {diag.root_cause}")
        print(f"  Confidence : {diag.confidence:.0%}")
        print(f"  Source     : {diag.source}")

    # ── Solution ────────────────────────────────────────────────────────────
    sol = full_result.get("solution_result")
    if sol:
        print(f"\n[Solution]  {sol.explanation}")
        for i, (step, cmd) in enumerate(
            zip(sol.steps, sol.commands + [None] * len(sol.steps)), 1
        ):
            print(f"  Step {i}: {step}")
            if cmd:
                print(f"    $ {cmd}")

    # ── Risk ─────────────────────────────────────────────────────────────────
    risk = full_result.get("security_check")
    if risk:
        print(f"\n[Risk] {risk.final_risk_level.value.upper()} — "
              f"{risk.message.strip()}")

    # ── Cache ────────────────────────────────────────────────────────────────
    s = get_cache_stats()
    print(f"\n[Cache] hit rate {s.hit_rate * 100:.1f}% "
          f"({s.hits}/{s.total} lookups, {s.misses} LLM call(s))")

    # ── History recording ────────────────────────────────────────────────────
    try:
        if classification is not None:
            applied = None
            if sol and sol.commands:
                applied = "; ".join(sol.commands)
            root_cause = diag.root_cause if diag else None
            get_history_manager().record_incident(
                raw_log=raw_log,
                category=classification.category,
                source=classification.source,
                severity=classification.severity.value,
                summary=classification.summary,
                root_cause=root_cause,
                applied_solution=applied,
                outcome="analyzed",
            )
    except Exception as hist_err:
        print(f"[History] WARNING: {hist_err}")


# ---------------------------------------------------------------------------
# Datetime parsing helper
# ---------------------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    """
    Parse ``YYYY-MM-DD HH:MM`` or bare ``HH:MM`` (uses today's date).
    Raises argparse.ArgumentTypeError on failure.
    """
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%H:%M":
                parsed = datetime.combine(date.today(), parsed.time())
            return parsed
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse {value!r}. Use 'YYYY-MM-DD HH:MM' or 'HH:MM'."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="logpulse",
        description="LogPulse AI — intelligent log analysis pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py --log 'OOMKilled: pod/worker exceeded 256Mi'\n"
            "  python main.py --file server.log --from '22:00' --to '22:30'\n"
            "  python main.py --stream mock --scenario oom_critical"
        ),
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--log",    metavar="TEXT",      help="Analyse a single raw log string")
    mode.add_argument("--file",   metavar="PATH",      help="Analyse a log file with a time filter")
    mode.add_argument("--stream", metavar="TYPE",      help="Analyse a simulated stream (use 'mock')")

    parser.add_argument(
        "--from", dest="from_dt", metavar="DATETIME",
        help="Start of time window for --file (YYYY-MM-DD HH:MM or HH:MM)",
    )
    parser.add_argument(
        "--to", dest="to_dt", metavar="DATETIME",
        help="End of time window for --file (YYYY-MM-DD HH:MM or HH:MM)",
    )
    parser.add_argument(
        "--scenario", metavar="NAME",
        help="Scenario name for --stream mock (e.g. oom_critical)",
    )

    args = parser.parse_args()

    # ── Validate: require at least one mode ────────────────────────────────
    if not any([args.log, args.file, args.stream]):
        parser.print_usage()
        print("\nRun with --help for full usage information.")
        sys.exit(0)

    # ── Mode 1: --log ───────────────────────────────────────────────────────
    if args.log:
        run_pipeline(args.log)
        return

    # ── Mode 2: --file ──────────────────────────────────────────────────────
    if args.file:
        if not args.from_dt or not args.to_dt:
            parser.error("--file requires both --from and --to")

        try:
            from_dt = _parse_dt(args.from_dt)
            to_dt   = _parse_dt(args.to_dt)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))

        if from_dt >= to_dt:
            parser.error("--from must be earlier than --to")

        filepath = Path(args.file)
        if not filepath.exists():
            parser.error(f"File not found: {filepath}")

        seen: set = set()
        lq = LogQueue()
        lq.start_consumer(run_pipeline)

        total, queued = read_file_logs(str(filepath), from_dt, to_dt, seen, lq.put)

        lq.set_received(total)
        lq.stop()
        lq.print_stats()
        return

    # ── Mode 3: --stream mock ───────────────────────────────────────────────
    if args.stream:
        if args.stream != "mock":
            parser.error(f"Unknown stream type {args.stream!r}. Use 'mock'.")

        if not args.scenario:
            parser.error("--stream mock requires --scenario")

        scenario_path = _SCENARIOS_DIR / f"{args.scenario}.json"
        if not scenario_path.exists():
            available = sorted(p.stem for p in _SCENARIOS_DIR.glob("*.json"))
            avail_str = ", ".join(available) if available else "(none found)"
            parser.error(
                f"Scenario '{args.scenario}' not found in {_SCENARIOS_DIR}.\n"
                f"Available scenarios: {avail_str}"
            )

        # Build the execution cluster state for this stream session.
        # Mutations from each pipeline run accumulate in this shared instance.
        global _cluster_state
        factory = _SCENARIO_CLUSTER_FACTORIES.get(args.scenario, seed_default_state)
        _cluster_state = factory()
        print(f"[Simulation] Cluster state ready: {args.scenario} ({type(_cluster_state).__name__})")

        seen: set = set()
        lq = LogQueue()
        lq.start_consumer(run_pipeline)

        total, queued = read_stream_mock(str(scenario_path), seen, lq.put)

        lq.set_received(total)
        lq.stop()
        lq.print_stats()
        _cluster_state = None  # reset shared state after the stream session ends


if __name__ == "__main__":
    main()
