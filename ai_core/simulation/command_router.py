"""
ai_core/simulation/command_router.py — Routes parsed commands to StateManager/renderers.

Supported command families:
  kubectl get pods / deployments / nodes
  kubectl describe pod <name>
  kubectl logs <name>
  kubectl rollout restart deployment/<name>
  kubectl set resources deployment/<name> --limits=memory=<X>
  kubectl delete pod <name>
  iptables -A INPUT -s <ip> -j DROP
  faillock --user <account> --reset
  systemctl status <service>
  free -h / free -m
  ps aux
  hdfs fsck /
  hdfs dfs -ls /
  echo / true / false                          → pass-through stubs
  Unknown                                      → exit_code 127
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

from sim_models import ClusterState
from state_manager import StateManager
import renderers


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
# kubectl sub-router
# ---------------------------------------------------------------------------

def _route_kubectl(cmd: str, state: ClusterState, mgr: StateManager) -> CommandResult:
    # kubectl get pods [-n <ns>]
    m = re.match(r"kubectl get pods?(?:\s+-n\s+(\S+))?", cmd)
    if m:
        ns = m.group(1) or "production"
        return CommandResult(stdout=renderers.render_pods(state, ns))

    # kubectl get deployments [-n <ns>]
    m = re.match(r"kubectl get deployments?(?:\s+-n\s+(\S+))?", cmd)
    if m:
        ns = m.group(1) or "production"
        return CommandResult(stdout=renderers.render_deployments(state, ns))

    # kubectl get nodes
    if re.match(r"kubectl get nodes?", cmd):
        return CommandResult(stdout=renderers.render_nodes(state))

    # kubectl describe pod <name>
    m = re.match(r"kubectl describe pod\s+(\S+)", cmd)
    if m:
        return CommandResult(stdout=renderers.render_pod_details(state, m.group(1)))

    # kubectl logs <name> [flags]
    m = re.match(r"kubectl logs\s+(\S+)", cmd)
    if m:
        return CommandResult(stdout=renderers.render_pod_logs(state, m.group(1)))

    # kubectl rollout restart deployment/<name>
    m = re.match(r"kubectl rollout restart deployment/(\S+)", cmd)
    if m:
        msg = mgr.restart_deployment(m.group(1))
        exit_code = 0 if "restarted" in msg else 1
        return CommandResult(stdout=msg if exit_code == 0 else "",
                             stderr="" if exit_code == 0 else msg,
                             exit_code=exit_code)

    # kubectl set resources deployment/<name> --limits=memory=<X>
    m = re.match(
        r"kubectl set resources deployment/(\S+).*--limits[= ]memory=(\S+)", cmd
    )
    if m:
        dep, limit = m.group(1), m.group(2)
        msg = mgr.set_memory_limit(dep, limit)
        exit_code = 0 if "updated" in msg else 1
        return CommandResult(stdout=msg if exit_code == 0 else "",
                             stderr="" if exit_code == 0 else msg,
                             exit_code=exit_code)

    # kubectl delete pod <name>
    m = re.match(r"kubectl delete pod\s+(\S+)(?:\s+-n\s+(\S+))?", cmd)
    if m:
        pod_name, ns = m.group(1), m.group(2) or "production"
        msg = mgr.delete_pod(pod_name, ns)
        exit_code = 0 if "deleted" in msg else 1
        return CommandResult(stdout=msg if exit_code == 0 else "",
                             stderr="" if exit_code == 0 else msg,
                             exit_code=exit_code)

    # kubectl rollout status deployment/<name>
    m = re.match(r"kubectl rollout status deployment/(\S+)", cmd)
    if m:
        dep_name = m.group(1)
        dep = state.deployments.get(dep_name)
        if dep:
            return CommandResult(
                stdout=f'deployment "{dep_name}" successfully rolled out'
            )
        return CommandResult(
            stderr=f'Error from server (NotFound): deployments.apps "{dep_name}" not found',
            exit_code=1,
        )

    # kubectl get all [-n <ns>]
    m = re.match(r"kubectl get all(?:\s+-n\s+(\S+))?", cmd)
    if m:
        ns = m.group(1) or "production"
        pods = renderers.render_pods(state, ns)
        deps = renderers.render_deployments(state, ns)
        return CommandResult(stdout=f"{pods}\n\n{deps}")

    # Unrecognised kubectl sub-command
    parts = cmd.split()
    return CommandResult(
        stderr=f"error: unknown command \"{' '.join(parts[1:3])}\" for \"kubectl\"",
        exit_code=1,
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
