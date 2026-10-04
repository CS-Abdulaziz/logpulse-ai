"""api/adapters/pipeline.py — Wraps ai_core without modifying it.

Calls each LangGraph node function individually in a background thread.
Emits SSE events to the IncidentStore after every stage.
Pauses before human_in_the_loop_node using a threading.Event.
The HTTP /approve and /reject handlers unblock that event.

Stage → SSE name mapping (matches frontend PipelineStepList):
  workflow_start_node          → "ingestion"
  cache_check + classifier     → "log_parsing"
  rag + history + merge        → "rag"
  diagnostic_agent_node        → "diagnosis"
  solution_agent_node          → "solution"
  risk_assessor_node           → "risk_assessment"
  ExecutionEngine (on approve) → "execution"
  workflow_complete_node       → "complete"
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from api.storage.memory import IncidentStore

# ── Project root (two levels above this file: api/adapters/ → project root) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_REPORTS_DIR  = _PROJECT_ROOT / "reports"
_HISTORY_DB   = _PROJECT_ROOT / "data" / "incident_history.sqlite3"


def _safe_dump(obj: Any) -> Optional[Dict[str, Any]]:
    """Pydantic model → plain dict, None-safe."""
    if obj is None:
        return None
    try:
        return obj.model_dump(mode="json")
    except Exception:
        return {"value": str(obj)}


class PipelineAdapter:
    """
    Runs the LogPulse pipeline for a single incident in a background thread.

    Do NOT call run() from an async context directly — use
    ``asyncio.get_event_loop().run_in_executor(executor, adapter.run, log, scenario)``
    """

    def __init__(self, incident_id: str, store: "IncidentStore") -> None:
        self.incident_id = incident_id
        self.store       = store

    # ── Public entry point ───────────────────────────────────────────────────

    def run(self, log: str, scenario: str = "default") -> None:
        try:
            self._run_pipeline(log, scenario)
        except Exception as exc:
            tb = traceback.format_exc()
            self.store.append_event(self.incident_id, "stage", {
                "name": "pipeline", "status": "error",
                "error": str(exc), "traceback": tb[:500],
            })
            self.store.update(self.incident_id, {"status": "failed"})

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _emit(self, event_type: str, data: dict) -> None:
        self.store.append_event(self.incident_id, event_type, data)

    def _stage(
        self,
        name:        str,
        status:      str,
        result:      Optional[dict] = None,
        duration_ms: Optional[int]  = None,
        error:       Optional[str]  = None,
    ) -> None:
        payload: dict = {"name": name, "status": status}
        if result      is not None: payload["result"]      = result
        if duration_ms is not None: payload["duration_ms"] = duration_ms
        if error       is not None: payload["error"]       = error
        self._emit("stage", payload)

    def _call_node(self, func, state) -> dict:
        """Call a node function and return its update dict (empty dict on None)."""
        result = func(state)
        return result if result else {}

    def _apply(self, state, updates: dict):
        """Apply a node's partial update dict to the LogState."""
        if not updates:
            return state
        return state.model_copy(update=updates)

    # ── Pipeline ─────────────────────────────────────────────────────────────

    def _run_pipeline(self, log: str, scenario: str) -> None:  # noqa: C901
        # ── Ensure UTF-8 stdout in this worker thread (Windows charmap fix) ─
        import sys as _sys
        if hasattr(_sys.stdout, "reconfigure"):
            try:
                _sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        if hasattr(_sys.stderr, "reconfigure"):
            try:
                _sys.stderr.reconfigure(encoding="utf-8")
            except Exception:
                pass

        # ── Lazy imports — keep ai_core out of module-load critical path ───
        from ai_core.workflow.graph import (
            workflow_start_node,
            workflow_complete_node,
            orchestrator_node,
            merge_context_node,
        )
        from ai_core.cache.cache_node import (
            cache_check_node,
            cache_write_node,
            route_after_cache,
        )
        from ai_core.workflow.agents.classifier_agent  import classifier_node
        from ai_core.workflow.agents.rag_agent         import rag_node
        from ai_core.workflow.agents.history_agent     import history_node
        from ai_core.workflow.agents.diagnostic_agent  import diagnostic_agent_node
        from ai_core.workflow.agents.solution_agent    import solution_agent_node
        from ai_core.workflow.agents.risk_assessor     import risk_assessor_node
        from ai_core.simulation.cluster_state          import seed_default_state
        from ai_core.workflow.state                    import LogState

        self.store.update(self.incident_id, {"status": "running"})

        # ── Build initial LogState ──────────────────────────────────────────
        cluster_state = seed_default_state()
        state = LogState(raw_log=log, cluster_state=cluster_state)
        self.store.update(self.incident_id, {"trace_id": state.trace_id})

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 1 — Ingestion (workflow_start_node)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("ingestion", "running")
        t0 = time.time()
        try:
            updates = self._call_node(workflow_start_node, state)
            state   = self._apply(state, updates)
            self._stage("ingestion", "done", duration_ms=_ms(t0))
        except Exception as exc:
            self._stage("ingestion", "error", error=str(exc))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 2 — Log Parsing & Structuring (cache lookup only)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("log_parsing", "running")
        t0 = time.time()
        cache_hit = False
        try:
            updates   = self._call_node(cache_check_node, state)
            state     = self._apply(state, updates)
            cache_hit = route_after_cache(state) == "cache_hit"
            self._stage("log_parsing", "done",
                        result={"cache_hit": cache_hit},
                        duration_ms=_ms(t0))
        except Exception as exc:
            self._stage("log_parsing", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 3 — Classification (Qwen LLM or cache replay)
        #   cache miss  → classifier_node (HTTP → Colab/Qwen) + cache_write
        #   cache hit   → classification already on state; skip LLM call
        #   orchestrator_node sets routing priority in both paths
        # ─────────────────────────────────────────────────────────────────────
        self._stage("classification", "running")
        t0 = time.time()
        try:
            if not cache_hit:
                updates = self._call_node(classifier_node, state)
                state   = self._apply(state, updates)
                self._call_node(cache_write_node, state)   # returns {}, no state change

            # Orchestrator (fast; sets routing priority context)
            updates = self._call_node(orchestrator_node, state)
            state   = self._apply(state, updates)

            cls      = state.classification
            source   = cls.source if cls else "unknown"
            # Derive a confidence proxy from what the classifier tells us:
            #   cache hit        → 1.0  (confirmed known pattern)
            #   successful LLM   → 0.85 (Qwen inference, no explicit score)
            #   fallback         → 0.0  (inference failed, keyword heuristic used)
            if cache_hit:
                confidence: float = 1.0
            elif cls and cls.source != "Fallback":
                confidence = 0.85
            else:
                confidence = 0.0

            cls_result = {
                "category":   cls.category   if cls else "Unknown",
                "severity":   cls.severity.value if cls else "Info",
                "summary":    cls.summary    if cls else "",
                "source":     source,
                "confidence": confidence,
                "cache_hit":  cache_hit,
            }
            self._stage("classification", "done", result=cls_result, duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"classification": cls_result})
        except Exception as exc:
            self._stage("classification", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 3 — Semantic Search (RAG + History + KnowledgeMerge)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("rag", "running")
        t0 = time.time()
        try:
            updates = self._call_node(rag_node, state)
            state   = self._apply(state, updates)

            updates = self._call_node(history_node, state)
            state   = self._apply(state, updates)

            updates = self._call_node(merge_context_node, state)
            state   = self._apply(state, updates)

            rag_result: dict = {}
            if state.rag_result:
                rag_result = {
                    "confidence":    state.rag_result.rag_confidence,
                    "playbook_found": bool(state.rag_result.playbook_steps),
                }
            self._stage("rag", "done", result=rag_result, duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"rag": rag_result})
        except Exception as exc:
            self._stage("rag", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 4 — Root Cause Synthesis (diagnostic_agent_node)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("diagnosis", "running")
        t0 = time.time()
        try:
            updates = self._call_node(diagnostic_agent_node, state)
            state   = self._apply(state, updates)

            diag_result: dict = {}
            if state.diagnostic_result:
                diag_result = {
                    "root_cause": state.diagnostic_result.root_cause,
                    "confidence": state.diagnostic_result.confidence,
                    "source":     state.diagnostic_result.source,
                }
            self._stage("diagnosis", "done", result=diag_result, duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"diagnosis": diag_result})
        except Exception as exc:
            self._stage("diagnosis", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 5 — Remediation Drafting (solution_agent_node)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("solution", "running")
        t0 = time.time()
        try:
            updates = self._call_node(solution_agent_node, state)
            state   = self._apply(state, updates)

            sol_result: dict = {}
            if state.solution_result:
                sol_result = {
                    "steps":       state.solution_result.steps,
                    "commands":    state.solution_result.commands,
                    "explanation": state.solution_result.explanation,
                    "source":      state.solution_result.source,
                }
            self._stage("solution", "done", result=sol_result, duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"solution": sol_result})
        except Exception as exc:
            self._stage("solution", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 6 — Security Assessment (risk_assessor_node)
        # ─────────────────────────────────────────────────────────────────────
        self._stage("risk_assessment", "running")
        t0 = time.time()
        try:
            updates = self._call_node(risk_assessor_node, state)
            state   = self._apply(state, updates)

            risk_result: dict = {}
            if state.security_check:
                risk_result = {
                    "risk_level":      state.security_check.final_risk_level.value,
                    "flagged_commands": state.security_check.flagged_commands,
                    "safe_commands":   state.security_check.safe_commands,
                    "message":         state.security_check.message,
                }
            self._stage("risk_assessment", "done", result=risk_result, duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"risk": risk_result})
        except Exception as exc:
            self._stage("risk_assessment", "error", error=str(exc), duration_ms=_ms(t0))
            self.store.update(self.incident_id, {"status": "failed"})
            return

        # ─────────────────────────────────────────────────────────────────────
        # PAUSE — Awaiting operator approval
        # Store state snapshot so /approve can access solution commands
        # ─────────────────────────────────────────────────────────────────────
        commands   = state.solution_result.commands if state.solution_result else []
        risk_level = state.security_check.final_risk_level.value if state.security_check else "unknown"

        self.store.update(self.incident_id, {
            "status":         "awaiting_approval",
            "state_snapshot": state,
        })
        self._emit("awaiting_approval", {
            "commands":    commands,
            "risk_level":  risk_level,
            "incident_id": self.incident_id,
        })

        # Block until /approve or /reject fires (5-minute timeout → auto-reject)
        approval_event = self.store.get_approval_event(self.incident_id)
        timed_out = not approval_event.wait(timeout=300)
        decision  = self.store.get_approval_decision(self.incident_id)

        if timed_out or decision != "approve":
            state = self._reject(state)
            self.store.update(self.incident_id, {"status": "rejected"})
            self._stage("execution", "done", result={"decision": "rejected"})
        else:
            # ─────────────────────────────────────────────────────────────────
            # STAGE 7 — Execution (bypasses human_in_the_loop_node's input())
            # ─────────────────────────────────────────────────────────────────
            self._stage("execution", "running")
            self.store.update(self.incident_id, {"status": "executing"})
            t0 = time.time()
            try:
                state, exec_result = self._execute(state)
                self._stage("execution", "done", result=exec_result, duration_ms=_ms(t0))
                self.store.update(self.incident_id, {"execution": exec_result})
            except Exception as exc:
                self._stage("execution", "error", error=str(exc), duration_ms=_ms(t0))

        # ─────────────────────────────────────────────────────────────────────
        # STAGE 8 — Complete (workflow_complete_node → PDF generation)
        # ─────────────────────────────────────────────────────────────────────
        try:
            updates = self._call_node(workflow_complete_node, state)
            state   = self._apply(state, updates)
        except Exception as exc:
            print(f"[Pipeline] workflow_complete_node error (non-fatal): {exc}")

        # Find generated PDF
        report_path = _find_report(_REPORTS_DIR, state.trace_id)
        duration_ms = state.execution.processing_time_ms or 0

        final_status = "completed" if decision == "approve" else "rejected"
        self.store.update(self.incident_id, {
            "status":      final_status,
            "report_path": report_path,
            "duration_ms": duration_ms,
        })

        self._emit("complete", {
            "incident_id": self.incident_id,
            "duration_ms": duration_ms,
            "report_path": report_path,
            "status":      final_status,
        })

    # ── Approval helpers ─────────────────────────────────────────────────────

    def _execute(self, state) -> tuple:
        """Execute approved commands directly via ExecutionEngine."""
        from ai_core.simulation.cluster_state import (
            ClusterState as ExecClusterState,
            seed_default_state,
        )
        from ai_core.simulation.execution_engine import ExecutionEngine
        from ai_core.simulation.intent_parser    import parse_kubectl
        from ai_core.workflow.state import (
            HumanReviewResult,
            OperatorDecision,
            SandboxResult,
        )
        from ai_core.events.recorder import record
        from ai_core.events.models   import EventType

        sol      = state.solution_result
        commands = sol.commands if sol else []

        exec_state = state.cluster_state
        if not isinstance(exec_state, ExecClusterState):
            exec_state = seed_default_state()

        engine          = ExecutionEngine(exec_state)
        sandbox_results: list = []
        all_mutations:   list = []
        cmd_summaries:   list = []

        for cmd in commands:
            try:
                intent = parse_kubectl(cmd)
                result = engine.execute(intent)

                sandbox_results.append(SandboxResult(
                    command=cmd,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    execution_time_ms=0,
                ))
                all_mutations.extend(result.state_mutations)
                cmd_summaries.append({
                    "command":         cmd,
                    "success":         result.success,
                    "exit_code":       result.exit_code,
                    "stdout":          result.stdout[:200],
                    "state_mutations": result.state_mutations,
                })

                # Emit per-command event so SSE clients can stream results live
                self._emit("command_result", {
                    "command":         cmd,
                    "success":         result.success,
                    "exit_code":       result.exit_code,
                    "stdout":          result.stdout,
                    "state_mutations": result.state_mutations,
                })

                ev = EventType.SANDBOX_EXECUTED if result.success else EventType.SANDBOX_FAILED
                record(
                    incident_id=state.trace_id,
                    event_type=ev,
                    node_name="api_execution",
                    message=f"{cmd[:80]} (exit {result.exit_code})",
                    metadata={"exit_code": result.exit_code, "mutations": result.state_mutations},
                )

            except Exception as exc:
                self._emit("command_result", {
                    "command": cmd, "success": False,
                    "exit_code": -1, "stdout": "", "state_mutations": [],
                    "error": str(exc),
                })

        review = HumanReviewResult(
            decision=OperatorDecision.APPROVED,
            operator_note="",
            sandbox_results=sandbox_results,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        new_state = state.model_copy(update={
            "user_approved": True,
            "human_review":  review,
        })

        exec_result = {
            "decision":        "approved",
            "commands_run":    len(cmd_summaries),
            "state_mutations": all_mutations,
            "results":         cmd_summaries,
        }
        return new_state, exec_result

    def _reject(self, state) -> object:
        """Build a rejected HumanReviewResult and return updated state."""
        from ai_core.workflow.state import (
            HumanReviewResult,
            OperatorDecision,
        )

        review = HumanReviewResult(
            decision=OperatorDecision.REJECTED,
            operator_note="",
            sandbox_results=[],
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        return state.model_copy(update={
            "user_approved": False,
            "human_review":  review,
        })


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _find_report(reports_dir: Path, trace_id: str) -> Optional[str]:
    """Glob for the PDF that workflow_complete_node just generated."""
    prefix = f"incident_{trace_id[:8]}_"
    matches = sorted(reports_dir.glob(f"{prefix}*.pdf"), reverse=True)
    return str(matches[0]) if matches else None
