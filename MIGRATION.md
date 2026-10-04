# Execution Intelligence Layer -- Migration Guide

## What changed

The sandbox execution layer has been refactored in two phases.

### Phase 1 -- Intent-based routing (previous session)

`ai_core/simulation/command_router.py` was rewritten to use a structured
parser instead of sequential `re.match` chains.  All kubectl commands are now
parsed into a `KubectlIntent` before dispatch, so no valid command returns
`exit -1` or falls through to an "unknown command" error.

### Phase 2 -- Execution Intelligence Layer (this session)

Three new files were added alongside the existing pipeline:

| File | Role |
|------|------|
| `ai_core/simulation/intent_parser.py` | Updated: `KubectlIntent` now uses `verb / resource / target / flags` instead of an enum. `parse_kubectl()` is the new entry point; `parse_kubectl_intent` is a backward-compatible alias. |
| `ai_core/simulation/cluster_state.py` | New standalone state model (`PodState`, `DeploymentState`, `NodeState`, `ClusterEvent`, `ClusterState`). Provides `snapshot()` / `restore()` for deterministic testing. |
| `ai_core/simulation/execution_engine.py` | New `ExecutionEngine` with a handler dispatch table and `ExecutionResult` (includes `state_mutations` list). Drop-in `simulate_command()` function. |

## Call site changes

### Old path (unchanged -- still works)

```python
from ai_core.workflow.agents.sandbox_executor import execute_in_sandbox

result = execute_in_sandbox(command, incident_id="...", cluster_state=sim_state)
# cluster_state is sim_models.ClusterState (Pydantic, loaded from JSON scenario)
```

This path is untouched.  `sandbox_executor` → `command_router` → intent-based
dispatch → `sim_models.ClusterState` mutations.

### New path

```python
from ai_core.simulation.cluster_state import seed_oom_state
from ai_core.simulation.execution_engine import simulate_command, ExecutionEngine

# Convenience wrapper
result = simulate_command("kubectl set resources deployment/worker-api --limits=memory=512Mi")

# Or with an explicit state (recommended for testing)
state  = seed_oom_state()
engine = ExecutionEngine(state)
result = engine.execute(parse_kubectl("kubectl rollout restart deployment/worker-api"))

print(result.stdout)
print(result.state_mutations)   # ["worker-api: OOMKilled → Running (restart #15)"]
```

## intent_parser.py -- API change

The `KubectlIntent` schema changed from enum-based to field-based.

| Old field | New field | Example |
|-----------|-----------|---------|
| `intent: KubectlIntentType.FETCH_LOGS` | `verb="logs"` | |
| `intent: KubectlIntentType.METRICS` | `verb="top", resource="pod"\|"node"` | |
| `intent: KubectlIntentType.ROLLOUT_RESTART` | `verb="rollout", resource="restart"` | |
| `intent: KubectlIntentType.ROLLOUT_STATUS` | `verb="rollout", resource="status"` | |
| `intent: KubectlIntentType.GET_POD` | `verb="get", resource="pod"` | |
| `options: dict` | `flags: dict` | `flags["tail"] = 100` |
| `options["resource"]` | `intent.resource` | `"pod"` or `"node"` |

`parse_kubectl_intent` is kept as an alias for `parse_kubectl` -- existing
imports continue to work, but the returned object has the new field names.

## No breaking changes to existing modules

- `sandbox_executor.py` -- unchanged
- `sim_models.py` -- unchanged (still loaded from JSON scenario files)
- `state_manager.py` -- unchanged (two new methods added: `scale_deployment`, `exec_pod`)
- `renderers.py` -- unchanged (five new renderers added: `render_top_pods`,
  `render_top_nodes`, `render_events`, `render_services`, `render_deployment_details`)
- All 177 pre-existing tests continue to pass
