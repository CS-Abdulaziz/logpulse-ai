"""
ai_core/simulation/command_router.py — Intent-based command router.

All kubectl commands are first parsed into a KubectlIntent (see
intent_parser.py) and dispatched by intent type.  This eliminates rigid
string-matching and ensures that no valid Kubernetes command ever returns
exit -1 or exit 127.

Supported intent families (kubectl)
-------------------------------------
  FETCH_LOGS         → render pod logs with tail/previous options
  METRICS            → render CPU/memory table (pod or node)
  ROLLOUT_STATUS     → report deployment health
  ROLLOUT_RESTART    → restart deployment pods, mutate state
  PATCH_DEPLOYMENT   → apply memory-limit patch, mutate state
  GET_POD            → render pod list or single-pod detail
  GET_DEPLOYMENT     → render deployment list
  GET_NODES          → render node table
  GET_ALL            → render pods + deployments
  GET_EVENTS         → render warning/normal event table
  GET_SERVICE        → render service table
  DESCRIBE_POD       → full pod detail
  DESCRIBE_DEPLOYMENT → full deployment detail
  SET_RESOURCES      → set memory limit, mutate state
  DELETE_POD         → delete pod, mutate state
  APPLY_MANIFEST     → acknowledged (no manifest state)
  SCALE              → scale replicas, mutate state
  EXEC               → simulate exec in pod
  UNKNOWN            → safe fallback, exit 0 with explanation

Non-kubectl families
---------------------
  iptables, faillock, systemctl, free, ps, hdfs, last/who/w,
  cat/tail/grep auth.log, echo/true/false/comments
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

from ai_core.simulation.sim_models import ClusterState
from ai_core.simulation.state_manager import StateManager
from ai_core.simulation import renderers
from ai_core.simulation.intent_parser import parse_kubectl


@dataclass
class CommandResult:
    stdout:    str = ""
    stderr:    str = ""
    exit_code: int = 0


def route(command: str, state: ClusterState) -> CommandResult:
    """
    Parse *command* and apply its effect to *state*, returning a CommandResult.

    All mutations happen on the shared ClusterState instance (in-place).
    """
    cmd = command.strip()
    if not cmd:
        return CommandResult()

    mgr = StateManager(state)

    # ── Trivial pass-throughs ────────────────────────────────────────────────
    if re.match(r"^(echo\b|true$|false$|#)", cmd):
        return CommandResult(stdout=_eval_echo(cmd))

    # ── kubectl ──────────────────────────────────────────────────────────────
    if cmd.startswith("kubectl "):
        return _route_kubectl(cmd, state, mgr)

    # ── iptables ─────────────────────────────────────────────────────────────
    m = re.match(
        r"iptables\s+-A\s+INPUT\s+-s\s+([\d.]+)\s+-j\s+DROP", cmd
    )
    if m:
        ip = m.group(1)
        err = mgr.block_ip(ip)
        if err:
            return CommandResult(stderr=err, exit_code=1)
        return CommandResult(stdout=f"Rule added: DROP from {ip}")

    # ── faillock ─────────────────────────────────────────────────────────────
    m = re.match(r"faillock\s+--user\s+(\S+)\s+--reset", cmd)
    if m:
        account = m.group(1)
        msg = mgr.unlock_account(account)
        return CommandResult(stdout=msg)

    # ── systemctl ────────────────────────────────────────────────────────────
    m = re.match(r"systemctl\s+(?:status|restart|start|stop)\s+(\S+)", cmd)
    if m:
        service = m.group(1).rstrip(".service")
        active  = "stop" not in cmd
        return CommandResult(stdout=renderers.render_service_status(service, active))

    # ── free ─────────────────────────────────────────────────────────────────
    if re.match(r"free(\s+-[hHmMkK])*$", cmd):
        return CommandResult(stdout=mgr.get_free_memory())

    # ── ps ───────────────────────────────────────────────────────────────────
    if cmd.startswith("ps "):
        return CommandResult(stdout=_ps_stub(state))

    # ── hdfs ─────────────────────────────────────────────────────────────────
    if cmd.startswith("hdfs "):
        return _route_hdfs(cmd, state, mgr)

    # ── last / who / w ───────────────────────────────────────────────────────
    if re.match(r"^(last|who|w)\b", cmd):
        return CommandResult(stdout=_auth_stub(state))

    # ── cat /var/log/auth.log ────────────────────────────────────────────────
    if re.match(r"(cat|tail|grep).*auth\.log", cmd):
        return CommandResult(stdout=renderers.render_ssh_auth_log(state))

    # ── Unknown ──────────────────────────────────────────────────────────────
    first_word = cmd.split()[0]
    return CommandResult(
        stderr=f"bash: {first_word}: command not found",
        exit_code=127,
    )


# ---------------------------------------------------------------------------
# kubectl sub-router — intent-based dispatch
# ---------------------------------------------------------------------------

def _route_kubectl(cmd: str, state: ClusterState, mgr: StateManager) -> CommandResult:
    """
    Parse *cmd* into a KubectlIntent and dispatch on (verb, resource).

    No valid Kubernetes command returns exit -1 or exit 127.
    Unrecognised sub-commands return a safe explanation at exit 0.
    """
    intent = parse_kubectl(cmd)
    verb   = intent.verb
    res    = intent.resource
    target = intent.target or ""
    ns     = intent.namespace
    flags  = intent.flags

    # ── logs ──────────────────────────────────────────────────────────────
    if verb == "logs":
        return CommandResult(stdout=renderers.render_pod_logs(state, target))

    # ── top ───────────────────────────────────────────────────────────────
    if verb == "top":
        if res == "node":
            return CommandResult(stdout=renderers.render_top_nodes(state, target))
        return CommandResult(stdout=renderers.render_top_pods(state, ns, target))

    # ── rollout ───────────────────────────────────────────────────────────
    if verb == "rollout":
        if res == "status":
            dep = state.deployments.get(target)
            if dep:
                ok_msg = (
                    f'deployment "{target}" successfully rolled out'
                    if dep.ready >= dep.replicas
                    else f'Waiting for deployment "{target}" rollout to finish: '
                         f'{dep.ready} of {dep.replicas} updated replicas are available...'
                )
                return CommandResult(stdout=ok_msg)
            return CommandResult(
                stderr=f'Error from server (NotFound): deployments.apps "{target}" not found',
                exit_code=1,
            )
        if res == "restart":
            msg = mgr.restart_deployment(target)
            ok  = "restarted" in msg
            return CommandResult(
                stdout=msg if ok else "", stderr="" if ok else msg,
                exit_code=0 if ok else 1,
            )

    # ── patch ─────────────────────────────────────────────────────────────
    if verb == "patch":
        dep = state.deployments.get(target)
        if dep is None:
            return CommandResult(
                stderr=f'Error from server (NotFound): deployments.apps "{target}" not found',
                exit_code=1,
            )
        mem_m = re.search(r'"memory"\s*:\s*"([^"]+)"', intent.original_command)
        if mem_m:
            msg = mgr.set_memory_limit(target, mem_m.group(1))
            ok  = "updated" in msg
            return CommandResult(
                stdout=f'deployment.apps/{target} patched' if ok else "",
                stderr="" if ok else msg,
                exit_code=0 if ok else 1,
            )
        return CommandResult(stdout=f'deployment.apps/{target} patched')

    # ── get ───────────────────────────────────────────────────────────────
    if verb == "get":
        if res == "pod":
            if target:
                pod = state.pods.get(target)
                if pod:
                    return CommandResult(stdout=renderers.render_pod_details(state, target))
                return CommandResult(
                    stderr=f'Error from server (NotFound): pods "{target}" not found',
                    exit_code=1,
                )
            return CommandResult(stdout=renderers.render_pods(state, ns))
        if res == "deployment":
            return CommandResult(stdout=renderers.render_deployments(state, ns))
        if res == "node":
            return CommandResult(stdout=renderers.render_nodes(state))
        if res == "all":
            return CommandResult(stdout=f"{renderers.render_pods(state, ns)}\n\n{renderers.render_deployments(state, ns)}")
        if res == "events":
            return CommandResult(stdout=renderers.render_events(state, ns))
        if res == "service":
            return CommandResult(stdout=renderers.render_services(state, ns))

    # ── describe ──────────────────────────────────────────────────────────
    if verb == "describe":
        if res == "pod":
            return CommandResult(stdout=renderers.render_pod_details(state, target))
        if res == "deployment":
            return CommandResult(stdout=renderers.render_deployment_details(state, target))

    # ── set resources ─────────────────────────────────────────────────────
    if verb == "set" and res == "resources":
        limits_str = str(flags.get("limits", ""))
        mem_m = re.search(r"memory=(\S+)", limits_str) or re.search(r"memory=(\S+)", intent.original_command)
        if mem_m:
            msg = mgr.set_memory_limit(target, mem_m.group(1))
            ok  = "updated" in msg
            return CommandResult(
                stdout=msg if ok else "", stderr="" if ok else msg,
                exit_code=0 if ok else 1,
            )
        return CommandResult(stderr="error: --limits flag required for set resources", exit_code=1)

    # ── delete pod ────────────────────────────────────────────────────────
    if verb == "delete" and res == "pod":
        msg = mgr.delete_pod(target, ns)
        ok  = "deleted" in msg
        return CommandResult(
            stdout=msg if ok else "", stderr="" if ok else msg,
            exit_code=0 if ok else 1,
        )

    # ── apply ─────────────────────────────────────────────────────────────
    if verb == "apply":
        file_hint = str(flags.get("filename", flags.get("f", "<manifest>")))
        return CommandResult(stdout=f"[simulation] applied {file_hint} (no manifest state in simulator)")

    # ── scale ─────────────────────────────────────────────────────────────
    if verb == "scale":
        replicas = flags.get("replicas")
        if replicas is None:
            return CommandResult(stderr="error: --replicas flag required", exit_code=1)
        msg = mgr.scale_deployment(target, int(replicas))
        ok  = "scaled" in msg
        return CommandResult(
            stdout=msg if ok else "", stderr="" if ok else msg,
            exit_code=0 if ok else 1,
        )

    # ── exec ──────────────────────────────────────────────────────────────
    if verb == "exec":
        exec_m = re.search(r"--\s+(.+)$", cmd)
        result = mgr.exec_pod(target, exec_m.group(1).strip() if exec_m else "")
        ok     = not result.startswith("error:") and "not found" not in result
        return CommandResult(
            stdout=result if ok else "", stderr="" if ok else result,
            exit_code=0 if ok else 1,
        )

    # ── safe fallback — never exit -1 ────────────────────────────────────
    subcmd = cmd.split()[1] if len(cmd.split()) > 1 else "?"
    return CommandResult(
        stdout=f"[simulation] kubectl {subcmd}: recognised but not simulated. No state mutated.",
        exit_code=0,
    )


# ---------------------------------------------------------------------------
# HDFS sub-router
# ---------------------------------------------------------------------------

def _route_hdfs(cmd: str, state: ClusterState, mgr: StateManager) -> CommandResult:
    if re.match(r"hdfs fsck", cmd):
        return CommandResult(stdout=mgr.run_hdfs_fsck())

    if re.match(r"hdfs dfs -ls", cmd):
        if state.hdfs is None:
            return CommandResult(stderr="HDFS not available", exit_code=1)
        lines = [f"Found {len(state.hdfs.datanodes)} DataNode(s):"]
        for dn in state.hdfs.datanodes:
            lines.append(f"  drwxr-xr-x   - hdfs supergroup   0 2025-01-15 22:00 {dn}")
        return CommandResult(stdout="\n".join(lines))

    return CommandResult(
        stderr=f"Unknown hdfs command: {cmd}",
        exit_code=1,
    )


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _eval_echo(cmd: str) -> str:
    if cmd.startswith("echo "):
        return cmd[5:].strip().strip('"').strip("'")
    return ""


def _ps_stub(state: ClusterState) -> str:
    lines = [
        "USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND",
        "root         1  0.0  0.1  19232  3304 ?        Ss   Jan15   0:01 /sbin/init",
        "root       123  0.1  0.5 156432 10240 ?        Ss   Jan15   0:12 sshd: /usr/sbin/sshd",
    ]
    if state.pods:
        for pod in list(state.pods.values())[:3]:
            lines.append(
                f"appuser   {1000 + len(pod.name) % 900:>5}  0.5  2.1 524288 43520 ?  "
                f"Ssl  Jan15   1:23 /app/server --pod={pod.name}"
            )
    return "\n".join(lines)


def _auth_stub(state: ClusterState) -> str:
    if state.ssh:
        ip = state.ssh.attacker_ip or "10.0.0.55"
        return (
            f"deploy   pts/0        {ip}       Wed Jan 15 22:00 - 22:30  (00:30)\n"
            f"root     pts/1        {ip}       Wed Jan 15 22:10 - still logged in"
        )
    return "No recent logins."
