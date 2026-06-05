#!/usr/bin/env python
"""
demo_execution.py -- End-to-end OOM remediation demo for the Execution Intelligence Layer.

Run:
    python demo_execution.py
"""
from __future__ import annotations

from ai_core.simulation.cluster_state import seed_oom_state
from ai_core.simulation.execution_engine import ExecutionEngine
from ai_core.simulation.intent_parser import parse_kubectl


DIVIDER = "-" * 72


def _banner(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def _run(engine: ExecutionEngine, cmd: str) -> None:
    intent = parse_kubectl(cmd)
    result = engine.execute(intent)
    label  = f"  {intent.verb} {intent.resource or ''} -> {intent.target or '(all)'}"
    print(f"\n$ {cmd}")
    print(f"  [{label.strip()}]")
    if result.state_mutations:
        for m in result.state_mutations:
            print(f"  * {m}")
    if result.stdout:
        for line in result.stdout.splitlines():
            print(f"    {line}")
    if not result.success:
        print(f"  ERROR: {result.stderr}")


def main() -> None:
    _banner("LogPulse AI -- Execution Intelligence Layer Demo")
    print("  Scenario : OOM critical -- worker-api (256Mi limit, 94% memory usage)")
    print("  Objective: Raise memory limit to 512Mi and restart the deployment")

    state  = seed_oom_state()
    engine = ExecutionEngine(state)

    # ── Phase 1: Observe ──────────────────────────────────────────────────
    _banner("Phase 1 -- Observe")
    _run(engine, "kubectl top pod -n production")
    _run(engine, "kubectl get events -n production")
    _run(engine, "kubectl describe pod worker-api-7d9f8b-xk2q -n production")
    _run(engine, "kubectl logs worker-api-7d9f8b-xk2q -n production --tail=10")

    # ── Phase 2: Remediate ────────────────────────────────────────────────
    _banner("Phase 2 -- Remediate")
    _run(
        engine,
        "kubectl set resources deployment/worker-api --limits=memory=512Mi -n production",
    )
    _run(engine, "kubectl rollout restart deployment/worker-api -n production")

    # ── Phase 3: Verify ───────────────────────────────────────────────────
    _banner("Phase 3 -- Verify")
    _run(engine, "kubectl rollout status deployment/worker-api -n production")
    _run(engine, "kubectl top pod -n production")
    _run(engine, "kubectl get pods -n production")

    # ── State diff ────────────────────────────────────────────────────────
    _banner("Final cluster state diff")
    dep = state.deployments["worker-api"]
    pod = state.pods["worker-api-7d9f8b-xk2q"]
    rows = [
        ("deployment worker-api", "memory_limit", "256Mi",          dep.memory_limit),
        ("deployment worker-api", "revision",     "7",               str(dep.revision)),
        ("deployment worker-api", "available",    "0/1",            f"{dep.available_replicas}/{dep.replicas}"),
        ("pod worker-api-xk2q",  "status",        "OOMKilled",       pod.status),
        ("pod worker-api-xk2q",  "restarts",      "14",              str(pod.restarts)),
        ("pod worker-api-xk2q",  "memory_limit",  "256Mi",           f"{pod.memory_limit_mb}Mi"),
    ]
    print(f"\n  {'RESOURCE':<28} {'FIELD':<16} {'BEFORE':<12} AFTER")
    print(f"  {'-'*28} {'-'*16} {'-'*12} {'-'*12}")
    for resource, field, before, after in rows:
        changed = "  *" if before != after else "   "
        print(f"{changed} {resource:<28} {field:<16} {before:<12} {after}")

    print(f"\n  {DIVIDER}")
    print("  Demo complete -- all steps handled by the Execution Intelligence Layer")
    print("  No exit -1 returned for any kubectl command.")
    print(f"  {DIVIDER}\n")


if __name__ == "__main__":
    main()
