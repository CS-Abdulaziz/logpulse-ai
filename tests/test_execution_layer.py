"""
tests/test_execution_layer.py
==============================
Full test suite for the Execution Intelligence Layer.

Test groups
-----------
PARSING (P-01 … P-20)   KubectlIntent field assertions for 20 command patterns.
MUTATION (M-01 … M-06)  State changes produced by mutating intents.
DETERMINISM (D-01)      Same intent on same snapshot → identical results.
NO-OP (N-01 … N-03)     Unsupported commands never return exit -1.
END-TO-END (E-01)       Full OOM remediation sequence with state assertions.
CLUSTER-STATE (C-01 … C-04) ClusterState model: snapshot/restore, events, helpers.

Run:
    pytest tests/test_execution_layer.py -v
"""
from __future__ import annotations

import pytest

from ai_core.simulation.cluster_state import (
    ClusterEvent,
    ClusterState,
    PodState,
    DeploymentState,
    NodeState,
    seed_oom_state,
    seed_default_state,
)
from ai_core.simulation.execution_engine import (
    ExecutionEngine,
    ExecutionResult,
    simulate_command,
)
from ai_core.simulation.intent_parser import KubectlIntent, parse_kubectl


# ===========================================================================
# Helpers
# ===========================================================================

def oom_engine() -> tuple[ExecutionEngine, ClusterState]:
    state  = seed_oom_state()
    engine = ExecutionEngine(state)
    return engine, state


# ===========================================================================
# PARSING — 20 cases
# ===========================================================================

class TestParsing:

    # P-01 bare logs
    def test_p01_logs_bare(self):
        i = parse_kubectl("kubectl logs worker-api")
        assert i.verb == "logs"
        assert i.target == "worker-api"
        assert i.resource is None

    # P-02 logs with namespace and --tail
    def test_p02_logs_namespace_tail(self):
        i = parse_kubectl("kubectl logs worker-api-7d9f8b-xk2q -n production --tail=100")
        assert i.verb == "logs"
        assert i.target == "worker-api-7d9f8b-xk2q"
        assert i.namespace == "production"
        assert i.flags.get("tail") == 100

    # P-03 logs --previous (long form)
    def test_p03_logs_previous(self):
        i = parse_kubectl("kubectl logs worker-api --previous")
        assert i.verb == "logs"
        assert i.flags.get("previous") is True

    # P-04 top pod (no name)
    def test_p04_top_pod_no_name(self):
        i = parse_kubectl("kubectl top pod")
        assert i.verb == "top"
        assert i.resource == "pod"
        assert i.target is None

    # P-05 top pod with name
    def test_p05_top_pod_named(self):
        i = parse_kubectl("kubectl top pod worker-api-7d9f8b-xk2q")
        assert i.verb == "top"
        assert i.resource == "pod"
        assert i.target == "worker-api-7d9f8b-xk2q"

    # P-06 top node
    def test_p06_top_node(self):
        i = parse_kubectl("kubectl top node")
        assert i.verb == "top"
        assert i.resource == "node"

    # P-07 get pods (plural alias)
    def test_p07_get_pods(self):
        i = parse_kubectl("kubectl get pods")
        assert i.verb == "get"
        assert i.resource == "pod"
        assert i.target is None

    # P-08 get pods with namespace
    def test_p08_get_pods_namespace(self):
        i = parse_kubectl("kubectl get pods -n kube-system")
        assert i.namespace == "kube-system"

    # P-09 get pod/name shorthand
    def test_p09_get_pod_slash_notation(self):
        i = parse_kubectl("kubectl get pod/worker-api-7d9f8b-xk2q")
        assert i.verb == "get"
        assert i.resource == "pod"
        assert i.target == "worker-api-7d9f8b-xk2q"

    # P-10 describe pod
    def test_p10_describe_pod(self):
        i = parse_kubectl("kubectl describe pod worker-api-7d9f8b-xk2q")
        assert i.verb == "describe"
        assert i.resource == "pod"
        assert i.target == "worker-api-7d9f8b-xk2q"

    # P-11 describe deployment slash notation
    def test_p11_describe_deployment_slash(self):
        i = parse_kubectl("kubectl describe deployment/worker-api")
        assert i.verb == "describe"
        assert i.resource == "deployment"
        assert i.target == "worker-api"

    # P-12 rollout restart deployment/name
    def test_p12_rollout_restart(self):
        i = parse_kubectl("kubectl rollout restart deployment/worker-api")
        assert i.verb == "rollout"
        assert i.resource == "restart"
        assert i.target == "worker-api"

    # P-13 rollout status deployment/name
    def test_p13_rollout_status(self):
        i = parse_kubectl("kubectl rollout status deployment/worker-api")
        assert i.verb == "rollout"
        assert i.resource == "status"
        assert i.target == "worker-api"

    # P-14 set resources with --limits=memory=
    def test_p14_set_resources(self):
        i = parse_kubectl(
            "kubectl set resources deployment/worker-api --limits=memory=512Mi"
        )
        assert i.verb == "set"
        assert i.resource == "resources"
        assert i.target == "worker-api"
        assert "512Mi" in str(i.flags.get("limits", ""))

    # P-15 scale deployment/name --replicas=N
    def test_p15_scale(self):
        i = parse_kubectl("kubectl scale deployment/worker-api --replicas=3")
        assert i.verb == "scale"
        assert i.target == "worker-api"
        assert i.flags.get("replicas") == 3

    # P-16 patch deployment (space-separated, no slash)
    def test_p16_patch_deployment_no_slash(self):
        i = parse_kubectl("kubectl patch deployment worker-api -p '{}'")
        assert i.verb == "patch"
        assert i.resource == "deployment"
        assert i.target == "worker-api"

    # P-17 delete pod with namespace
    def test_p17_delete_pod(self):
        i = parse_kubectl(
            "kubectl delete pod worker-api-7d9f8b-xk2q -n production"
        )
        assert i.verb == "delete"
        assert i.resource == "pod"
        assert i.target == "worker-api-7d9f8b-xk2q"
        assert i.namespace == "production"

    # P-18 apply -f
    def test_p18_apply(self):
        i = parse_kubectl("kubectl apply -f deployment.yaml")
        assert i.verb == "apply"
        assert i.flags.get("filename") == "deployment.yaml"

    # P-19 exec with -- separator
    def test_p19_exec(self):
        i = parse_kubectl("kubectl exec worker-api-7d9f8b-xk2q -- bash")
        assert i.verb == "exec"
        assert i.target == "worker-api-7d9f8b-xk2q"

    # P-20 completely unknown / malformed → verb="unknown"
    def test_p20_unknown_verb(self):
        i = parse_kubectl("kubectl frobnicateXYZ --magic")
        assert i.verb == "unknown"
        assert i.original_command != ""


# ===========================================================================
# MUTATION — state changes
# ===========================================================================

class TestMutations:

    # M-01 set resources changes deployment memory_limit
    def test_m01_set_resources_memory(self):
        engine, state = oom_engine()
        r = engine.execute(
            parse_kubectl("kubectl set resources deployment/worker-api --limits=memory=512Mi")
        )
        assert r.success
        assert r.exit_code == 0
        dep = state.deployments["worker-api"]
        assert dep.memory_limit == "512Mi"

    # M-02 set resources increments revision
    def test_m02_set_resources_revision(self):
        engine, state = oom_engine()
        old_rev = state.deployments["worker-api"].revision
        engine.execute(
            parse_kubectl("kubectl set resources deployment/worker-api --limits=memory=512Mi")
        )
        assert state.deployments["worker-api"].revision == old_rev + 1

    # M-03 set resources updates pod memory_limit_mb
    def test_m03_set_resources_updates_pod(self):
        engine, state = oom_engine()
        engine.execute(
            parse_kubectl("kubectl set resources deployment/worker-api --limits=memory=512Mi")
        )
        pod = state.pods["worker-api-7d9f8b-xk2q"]
        assert pod.memory_limit_mb == 512

    # M-04 rollout restart sets pod status to Running
    def test_m04_rollout_restart_pod_status(self):
        engine, state = oom_engine()
        r = engine.execute(
            parse_kubectl("kubectl rollout restart deployment/worker-api")
        )
        assert r.success
        pod = state.pods["worker-api-7d9f8b-xk2q"]
        assert pod.status == "Running"

    # M-05 rollout restart increments pod restarts
    def test_m05_rollout_restart_increments_restarts(self):
        engine, state = oom_engine()
        old_restarts = state.pods["worker-api-7d9f8b-xk2q"].restarts
        engine.execute(parse_kubectl("kubectl rollout restart deployment/worker-api"))
        assert state.pods["worker-api-7d9f8b-xk2q"].restarts == old_restarts + 1

    # M-06 scale changes replicas
    def test_m06_scale_replicas(self):
        engine, state = oom_engine()
        r = engine.execute(
            parse_kubectl("kubectl scale deployment/worker-api --replicas=3")
        )
        assert r.success
        assert state.deployments["worker-api"].replicas == 3
        assert len(r.state_mutations) > 0

    # M-07 patch with memory JSON mutates deployment
    def test_m07_patch_memory(self):
        engine, state = oom_engine()
        r = engine.execute(parse_kubectl(
            'kubectl patch deployment/worker-api -p '
            '\'{"spec":{"template":{"spec":{"containers":[{"name":"app","resources":'
            '{"limits":{"memory":"512Mi"}}}]}}}}\''
        ))
        assert r.success
        assert state.deployments["worker-api"].memory_limit == "512Mi"

    # M-08 delete pod marks pod as Pending
    def test_m08_delete_pod_pending(self):
        engine, state = oom_engine()
        r = engine.execute(
            parse_kubectl("kubectl delete pod worker-api-7d9f8b-xk2q -n production")
        )
        assert r.success
        assert state.pods["worker-api-7d9f8b-xk2q"].status == "Pending"

    # M-09 state_mutations list is non-empty for mutating intents
    def test_m09_mutations_list_populated(self):
        engine, state = oom_engine()
        r = engine.execute(
            parse_kubectl("kubectl set resources deployment/worker-api --limits=memory=512Mi")
        )
        assert len(r.state_mutations) >= 1


# ===========================================================================
# DETERMINISM
# ===========================================================================

class TestDeterminism:

    # D-01 same read-only intent from identical snapshots → identical stdout
    def test_d01_read_only_deterministic(self):
        state = seed_oom_state()
        snap  = state.snapshot()

        r1 = ExecutionEngine(state).execute(parse_kubectl("kubectl top pod"))
        state.restore(snap)
        r2 = ExecutionEngine(state).execute(parse_kubectl("kubectl top pod"))

        assert r1.stdout == r2.stdout
        assert r1.exit_code == r2.exit_code

    # D-02 snapshot round-trip preserves pod status after mutation
    def test_d02_restore_after_mutation(self):
        state = seed_oom_state()
        snap  = state.snapshot()

        ExecutionEngine(state).execute(
            parse_kubectl("kubectl rollout restart deployment/worker-api")
        )
        assert state.pods["worker-api-7d9f8b-xk2q"].status == "Running"

        state.restore(snap)
        assert state.pods["worker-api-7d9f8b-xk2q"].status == "OOMKilled"


# ===========================================================================
# NO-OP — never exit -1
# ===========================================================================

class TestNoop:

    # N-01 completely unsupported verb
    def test_n01_unsupported_verb(self):
        engine, _ = oom_engine()
        r = engine.execute(parse_kubectl("kubectl wait --for=condition=ready pod/x"))
        assert r.exit_code == 0, "Unsupported verb must not return exit -1"
        assert r.state_mutations == []

    # N-02 unsupported verb with explicit noop reason
    def test_n02_noop_has_reason(self):
        r = simulate_command("kubectl cordon worker-1")
        assert r.exit_code == 0
        assert "no-op" in r.stdout.lower() or "simulation" in r.stdout.lower()

    # N-03 malformed command never raises, returns exit 0
    def test_n03_malformed_never_raises(self):
        try:
            r = simulate_command("kubectl !!@@##$$")
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"simulate_command raised {exc!r} on malformed input")
        assert r.exit_code == 0


# ===========================================================================
# END-TO-END — OOM remediation sequence
# ===========================================================================

class TestEndToEnd:

    def test_e01_oom_remediation_sequence(self):
        state  = seed_oom_state()
        engine = ExecutionEngine(state)

        # Step 1: inspect metrics
        r1 = engine.execute(parse_kubectl("kubectl top pod -n production"))
        assert r1.success and r1.exit_code == 0
        assert "worker-api-7d9f8b-xk2q" in r1.stdout
        assert "CPU(cores)" in r1.stdout

        # Step 2: describe the failing pod
        r2 = engine.execute(
            parse_kubectl("kubectl describe pod worker-api-7d9f8b-xk2q -n production")
        )
        assert r2.success
        assert "OOMKilled" in r2.stdout
        assert "256Mi" in r2.stdout

        # Step 3: fetch crash logs
        r3 = engine.execute(
            parse_kubectl("kubectl logs worker-api-7d9f8b-xk2q -n production --tail=20")
        )
        assert r3.success
        assert "OOMKilled" in r3.stdout or "Memory" in r3.stdout

        # Step 4: increase memory limit
        r4 = engine.execute(
            parse_kubectl(
                "kubectl set resources deployment/worker-api "
                "--limits=memory=512Mi -n production"
            )
        )
        assert r4.success
        assert state.deployments["worker-api"].memory_limit == "512Mi"
        assert state.deployments["worker-api"].revision == 8   # was 7
        assert state.pods["worker-api-7d9f8b-xk2q"].memory_limit_mb == 512

        # Step 5: rollout restart
        r5 = engine.execute(
            parse_kubectl("kubectl rollout restart deployment/worker-api -n production")
        )
        assert r5.success
        pod = state.pods["worker-api-7d9f8b-xk2q"]
        assert pod.status == "Running"
        assert state.deployments["worker-api"].available_replicas == 1
        assert state.deployments["worker-api"].revision == 9   # was 8

        # Step 6: confirm rollout completed
        r6 = engine.execute(
            parse_kubectl("kubectl rollout status deployment/worker-api -n production")
        )
        assert r6.success
        assert "successfully rolled out" in r6.stdout


# ===========================================================================
# CLUSTER-STATE — model tests
# ===========================================================================

class TestClusterState:

    # C-01 seed_oom_state has expected shape
    def test_c01_seed_oom_state_shape(self):
        state = seed_oom_state()
        assert "worker-api-7d9f8b-xk2q" in state.pods
        assert "worker-api" in state.deployments
        assert "worker-1" in state.nodes
        pod = state.pods["worker-api-7d9f8b-xk2q"]
        assert pod.status == "OOMKilled"
        assert pod.restarts == 14
        dep = state.deployments["worker-api"]
        assert dep.replicas == 1
        assert dep.available_replicas == 0

    # C-02 seed_default_state has 3 deployments, 5 pods, 2 nodes
    def test_c02_seed_default_state_shape(self):
        state = seed_default_state()
        assert len(state.deployments) == 3
        assert len(state.pods) >= 5
        assert len(state.nodes) == 2

    # C-03 snapshot / restore round-trip
    def test_c03_snapshot_restore(self):
        state = seed_oom_state()
        snap  = state.snapshot()

        state.pods["worker-api-7d9f8b-xk2q"].status = "Running"
        state.deployments["worker-api"].revision    = 99
        assert state.pods["worker-api-7d9f8b-xk2q"].status == "Running"

        state.restore(snap)
        assert state.pods["worker-api-7d9f8b-xk2q"].status == "OOMKilled"
        assert state.deployments["worker-api"].revision    == 7

    # C-04 log_event appends and is visible via get events
    def test_c04_log_event_visible(self):
        engine, state = oom_engine()
        state.log_event(ClusterEvent(
            timestamp="1m", event_type="Warning",
            reason="TestReason", object_kind="Pod",
            object_name="worker-api-7d9f8b-xk2q",
            message="synthetic test event",
        ))
        r = engine.execute(parse_kubectl("kubectl get events -n production"))
        assert r.exit_code == 0
        assert "TestReason" in r.stdout or "synthetic" in r.stdout

    # C-05 pods_for_deployment helper
    def test_c05_pods_for_deployment(self):
        state = seed_oom_state()
        pods  = state.pods_for_deployment("worker-api")
        assert len(pods) == 1
        assert pods[0].name == "worker-api-7d9f8b-xk2q"

        empty = state.pods_for_deployment("nonexistent")
        assert empty == []

    # C-06 simulate_command convenience wrapper
    def test_c06_simulate_command_convenience(self):
        state = seed_oom_state()
        r     = simulate_command("kubectl get pods", state)
        assert r.exit_code == 0
        assert "worker-api-7d9f8b-xk2q" in r.stdout

    # C-07 ExecutionResult constructors
    def test_c07_result_constructors(self):
        intent = parse_kubectl("kubectl get pods")
        ok  = ExecutionResult.ok(intent, "hello", mutations=["foo → bar"])
        assert ok.success and ok.exit_code == 0
        assert ok.state_mutations == ["foo → bar"]

        err = ExecutionResult.error(intent, "something went wrong", exit_code=2)
        assert not err.success and err.exit_code == 2

        noop = ExecutionResult.noop(intent, "no handler")
        assert noop.success and noop.exit_code == 0
        assert noop.state_mutations == []
