"""
ai_core/workflow/agents/context_resolver.py
============================================
Context-aware placeholder resolution for LogPulse remediation commands.

Problem
-------
The LogPulse playbook library stores resolution steps as human-readable
runbook templates that deliberately contain ``<placeholder>`` tokens, e.g.::

    kubectl rollout restart deployment/<deployment-name>
    iptables -A INPUT -s <ip> -j DROP
    faillock --user <account> --reset

When ``_parse_steps_from_playbook`` extracts these commands verbatim,
the simulation layer (``command_router.py``) receives un-filled templates
and returns exit_code=-1 or "NotFound" errors — the cluster state never
mutates.  The LLM path can exhibit the same symptom when context is thin.

Solution
--------
This module provides two public functions:

``extract_context(state)``
    Mines a ``LogState`` for named entities — pod name, deployment name,
    IP address, locked account, etc. — using a priority chain:

        1. ``cluster_state``  — ground-truth simulation data (highest confidence)
        2. ``raw_log``        — targeted regex extraction
        3. ``diagnostic_result.root_cause`` — secondary evidence

``resolve_placeholders(commands, ctx)``
    Iterates over the command list and replaces every recognised
    ``<placeholder>`` token with the corresponding ``IncidentContext`` value.

Safety rules
------------
- Only *recognised* placeholder names are replaced (see ``_PLACEHOLDER_MAP``).
- Extracted values are run through a strict safety pattern
  (``^[a-zA-Z0-9_./:@-]+$``) before injection — anything with shell
  metacharacters (``;``, ``|``, ``&``, ``$``, etc.) is rejected and the
  original placeholder is kept.
- Placeholders with no confident match are left **unchanged**.
- The function never raises; failures are absorbed and the original command
  is returned as-is.

Supported placeholder names
---------------------------
``<pod-name>``        ``<pod_name>``        ``<pod>``
``<deployment-name>`` ``<deployment_name>`` ``<deployment>``
``<container-name>``  ``<container_name>``  ``<container>``
``<service-name>``    ``<service_name>``    ``<service>``
``<namespace>``       ``<ns>``
``<node-name>``       ``<node_name>``       ``<node>``
``<ip>``              ``<ip-address>``      ``<ip_address>``
``<attacker-ip>``     ``<source-ip>``
``<account>``         ``<user>``            ``<username>``
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from ai_core.workflow.state import LogState


# ---------------------------------------------------------------------------
# Entity container
# ---------------------------------------------------------------------------

@dataclass
class IncidentContext:
    """Structured entities extracted from a ``LogState``."""
    pod_name:        Optional[str] = None
    deployment_name: Optional[str] = None
    container_name:  Optional[str] = None   # typically same as deployment
    namespace:       Optional[str] = None
    node_name:       Optional[str] = None
    service_name:    Optional[str] = None
    attacker_ip:     Optional[str] = None
    locked_account:  Optional[str] = None
    memory_limit:    Optional[str] = None   # informational; not a placeholder


# ---------------------------------------------------------------------------
# Safety & utility patterns
# ---------------------------------------------------------------------------

# Only inject values that cannot cause shell injection.
# Allows: alphanumeric, dash, underscore, dot, slash, colon, @
_SAFE_VALUE_RE = re.compile(r"^[a-zA-Z0-9_./:@-]+$")

# Memory: e.g. 256Mi, 4Gi, 512Ki
_MEM_RE = re.compile(r"\b(\d+(?:Mi|Gi|Ki|Ti))\b")

# IPv4 address
_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# Kubernetes hash suffix on pod names:
# strips 1–2 trailing dash-separated segments that look like auto-generated
# hashes (contain at least one digit somewhere, 2–12 total chars), e.g.:
#   worker-api-7d9f8b-xk2q  →  worker-api  (two segments stripped)
#   nginx-7c9f5b             →  nginx       (one segment stripped)
#   worker-api               →  worker-api  (no suffix → unchanged)
#   worker-api-v2            →  worker-api-v2 (only 2 chars, min is 3 → unchanged)
#   nginx-deployment         →  nginx-deployment (no digits → unchanged)
#
# Pattern breakdown: each segment is a dash followed by a mix of lowercase
# alphanumeric chars that contains at least one digit ([a-z0-9]*\d[a-z0-9]*)
# and is between 3 and 12 characters long total.
_K8S_HASH_RE = re.compile(r"(-[a-z0-9]*\d[a-z0-9]{1,11}){1,2}$")


def _safe(value: Optional[str]) -> Optional[str]:
    """Return *value* if it passes the injection-safety check, else ``None``."""
    if value is None:
        return None
    v = value.strip()
    return v if (v and _SAFE_VALUE_RE.match(v)) else None


def _strip_k8s_hash(name: str) -> str:
    """
    Strip auto-generated Kubernetes hash suffixes from a pod name to derive
    the controlling Deployment/ReplicaSet name.

    Examples::
        worker-api-7d9f8b-xk2q  →  worker-api
        nginx-7c9f5b             →  nginx
        worker-api               →  worker-api   (no suffix → unchanged)
        worker-api-v2            →  worker-api-v2 (version tag → unchanged)
    """
    return _K8S_HASH_RE.sub("", name)


# ---------------------------------------------------------------------------
# Individual entity extractors
# ---------------------------------------------------------------------------

def _pod_from_log(log: str) -> Optional[str]:
    """
    Extract a pod name from patterns such as::

        pod/worker-api         →  worker-api
        pods/worker-api-7d9f8b-xk2q  →  worker-api-7d9f8b-xk2q
        pod: worker-api        →  worker-api
        pod worker-api         →  worker-api
    """
    m = re.search(
        r"\bpods?[/:\s]+([a-zA-Z][a-zA-Z0-9_-]+(?:-[a-zA-Z0-9]+)*)",
        log,
        re.IGNORECASE,
    )
    return m.group(1).rstrip(".") if m else None


def _service_from_log_prefix(log: str) -> Optional[str]:
    """
    Many syslog lines begin with ``sshd: …`` or ``nginx[1234]: …``.
    Return the service name when the prefix matches that pattern.
    """
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]+)(?:\[\d+\])?:\s", log.strip())
    if m:
        svc = m.group(1)
        _LEVEL_WORDS = {"INFO", "WARN", "WARNING", "ERROR", "FATAL", "DEBUG", "TRACE"}
        if svc.upper() not in _LEVEL_WORDS:
            return svc
    return None


def _attacker_ip_from_log(log: str) -> Optional[str]:
    """
    Prefer an IP that appears near failure/attack keywords.
    Falls back to the first non-loopback IP found in the line.
    """
    # Contextual: "from <ip>", "attacker <ip>", "blocked <ip>", "Failed … <ip>"
    m = re.search(
        r"(?:from|source|attacker|blocked?|fail(?:ed)?|attempt)\s+"
        r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})",
        log,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Generic fallback: first IP that isn't localhost
    for ip in _IP_RE.findall(log):
        if not ip.startswith("127.") and ip != "0.0.0.0":
            return ip
    return None


def _account_from_log(log: str) -> Optional[str]:
    """
    Extract a user/account name from patterns like::

        for root from 10.0.0.55
        for invalid user admin from …
        account root locked
        user deploy
    """
    m = re.search(
        r"(?:for\s+(?:invalid\s+user\s+)?|user\s+|account\s+)"
        r"([a-zA-Z][a-zA-Z0-9_]{1,31})"
        r"(?:\s+from|\s+locked|\s+failed|\s+from|$)",
        log,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1)
        _NOISE = {"password", "ssh2", "port", "the", "invalid", "for", "from"}
        if name.lower() not in _NOISE:
            return name
    return None


def _namespace_from_log(log: str) -> Optional[str]:
    """Extract a namespace from ``-n <ns>`` or ``namespace <ns>`` patterns."""
    m = re.search(
        r"(?:namespace[s]?[:\s/]+|-n\s+)([a-z][a-z0-9-]+)",
        log,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _node_from_log(log: str) -> Optional[str]:
    """Extract a node name from ``on node worker-3`` or ``node/worker-3``."""
    m = re.search(
        r"(?:on\s+node\s+|node[/:\s]+)([a-zA-Z][a-zA-Z0-9_-]+)",
        log,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _memory_from_log(log: str) -> Optional[str]:
    """Return the largest memory value found in the log (likely the limit)."""
    matches = _MEM_RE.findall(log)
    if not matches:
        return None

    def _to_bytes(s: str) -> int:
        n = int(re.match(r"\d+", s).group())
        if "Gi" in s:
            return n * (1024 ** 3)
        if "Mi" in s:
            return n * (1024 ** 2)
        if "Ki" in s:
            return n * 1024
        return n

    return max(matches, key=_to_bytes)


# ---------------------------------------------------------------------------
# Public: extract_context
# ---------------------------------------------------------------------------

def extract_context(state: LogState) -> IncidentContext:
    """
    Build an ``IncidentContext`` from the available ``LogState`` fields.

    Priority chain (highest confidence first):

    1. **cluster_state** — ground-truth simulation data attached to the state
       object.  Provides exact deployment / pod / SSH names used in the
       simulation layer.

    2. **raw_log** — targeted regex extraction from the original log line.

    3. **diagnostic_result.root_cause** — secondary evidence when the raw
       log alone is insufficient.
    """
    ctx = IncidentContext()
    log = state.raw_log or ""

    # ── 1. cluster_state (highest confidence) ─────────────────────────────
    cs = getattr(state, "cluster_state", None)
    if cs is not None:
        if getattr(cs, "deployments", None):
            first = next(iter(cs.deployments.values()))
            ctx.deployment_name = _safe(getattr(first, "name", None))
            ctx.namespace       = _safe(getattr(first, "namespace", None)) or "production"
        if getattr(cs, "pods", None):
            first_pod = next(iter(cs.pods.values()))
            ctx.pod_name  = _safe(getattr(first_pod, "name", None))
            ctx.node_name = _safe(getattr(first_pod, "node", None))
        ssh = getattr(cs, "ssh", None)
        if ssh is not None:
            ctx.attacker_ip = _safe(getattr(ssh, "attacker_ip", None)) or None
            locked = getattr(ssh, "locked_accounts", [])
            if locked:
                ctx.locked_account = _safe(locked[0])

    # ── 2. raw_log ─────────────────────────────────────────────────────────
    if ctx.pod_name is None:
        ctx.pod_name = _safe(_pod_from_log(log))

    if ctx.deployment_name is None:
        if ctx.pod_name:
            ctx.deployment_name = _safe(_strip_k8s_hash(ctx.pod_name))
        else:
            # "deployment/worker-api" may appear in the log itself
            m = re.search(r"deployment/([a-zA-Z][a-zA-Z0-9_-]+)", log)
            if m:
                ctx.deployment_name = _safe(m.group(1))

    # container_name defaults to deployment_name in Kubernetes
    if ctx.container_name is None and ctx.deployment_name:
        ctx.container_name = ctx.deployment_name

    if ctx.namespace is None:
        ctx.namespace = _safe(_namespace_from_log(log)) or "production"

    if ctx.node_name is None:
        ctx.node_name = _safe(_node_from_log(log))

    # service_name: syslog prefix takes precedence, then deployment_name
    svc = _safe(_service_from_log_prefix(log))
    ctx.service_name = svc or ctx.deployment_name

    if ctx.attacker_ip is None:
        ctx.attacker_ip = _safe(_attacker_ip_from_log(log))

    if ctx.locked_account is None:
        ctx.locked_account = _safe(_account_from_log(log))

    ctx.memory_limit = _safe(_memory_from_log(log))

    # ── 3. diagnostic_result (secondary) ──────────────────────────────────
    diag = getattr(state, "diagnostic_result", None)
    if diag is not None:
        rc = getattr(diag, "root_cause", "") or ""
        if ctx.pod_name is None:
            ctx.pod_name = _safe(_pod_from_log(rc))
        if ctx.deployment_name is None and ctx.pod_name:
            ctx.deployment_name = _safe(_strip_k8s_hash(ctx.pod_name))

    return ctx


# ---------------------------------------------------------------------------
# Placeholder → IncidentContext field mapping
# ---------------------------------------------------------------------------

#: Maps every supported placeholder name (lowercase, inner text of ``<…>``)
#: to the corresponding ``IncidentContext`` attribute name.
_PLACEHOLDER_MAP: Dict[str, str] = {
    # Pod
    "pod-name":        "pod_name",
    "pod_name":        "pod_name",
    "podname":         "pod_name",
    "pod":             "pod_name",
    # Deployment
    "deployment-name": "deployment_name",
    "deployment_name": "deployment_name",
    "deploymentname":  "deployment_name",
    "deployment":      "deployment_name",
    # Container
    "container-name":  "container_name",
    "container_name":  "container_name",
    "containername":   "container_name",
    "container":       "container_name",
    # Service
    "service-name":    "service_name",
    "service_name":    "service_name",
    "servicename":     "service_name",
    "service":         "service_name",
    # Namespace
    "namespace":       "namespace",
    "ns":              "namespace",
    # Node
    "node-name":       "node_name",
    "node_name":       "node_name",
    "nodename":        "node_name",
    "node":            "node_name",
    # IP / network
    "ip":              "attacker_ip",
    "ip-address":      "attacker_ip",
    "ip_address":      "attacker_ip",
    "attacker-ip":     "attacker_ip",
    "attacker_ip":     "attacker_ip",
    "source-ip":       "attacker_ip",
    "source_ip":       "attacker_ip",
    # Account / user
    "account":         "locked_account",
    "user":            "locked_account",
    "username":        "locked_account",
    "user-name":       "locked_account",
    "user_name":       "locked_account",
}

# Matches any ``<placeholder>`` token (non-greedy, single-line)
_PLACEHOLDER_RE = re.compile(r"<([^>\n]+)>")


# ---------------------------------------------------------------------------
# Public: resolve_placeholders
# ---------------------------------------------------------------------------

def resolve_placeholders(commands: List[str], ctx: IncidentContext) -> List[str]:
    """
    Replace ``<placeholder>`` tokens in *commands* with values from *ctx*.

    Contract
    --------
    - Recognised placeholder names (``_PLACEHOLDER_MAP``) are replaced when
      the corresponding ``IncidentContext`` field is non-``None``.
    - Unrecognised placeholder names are left **unchanged** (e.g.
      ``<manifest-file>``, ``<asg-name>``).
    - Values that fail the safety check are **not injected** — the original
      placeholder token is preserved for human inspection.
    - The function never raises.

    Parameters
    ----------
    commands:
        List of command strings, possibly containing ``<placeholder>`` tokens.
    ctx:
        ``IncidentContext`` built by ``extract_context()``.

    Returns
    -------
    A new list of command strings with placeholders substituted where possible.
    """
    return [_resolve_one(cmd, ctx) for cmd in commands]


def _resolve_one(cmd: str, ctx: IncidentContext) -> str:
    """Apply placeholder resolution to a single command string."""

    def _replace(m: re.Match) -> str:
        inner = m.group(1).strip().lower()
        attr  = _PLACEHOLDER_MAP.get(inner)
        if attr is None:
            return m.group(0)          # unrecognised → keep as-is
        value = getattr(ctx, attr, None)
        if value is None:
            return m.group(0)          # no confident value → keep placeholder
        # Final safety gate — should already be clean but double-check
        sv = str(value).strip()
        if not sv or not _SAFE_VALUE_RE.match(sv):
            return m.group(0)
        return sv

    try:
        return _PLACEHOLDER_RE.sub(_replace, cmd)
    except Exception:
        return cmd                     # never raise
