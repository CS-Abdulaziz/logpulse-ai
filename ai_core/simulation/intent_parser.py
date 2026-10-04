"""
ai_core/simulation/intent_parser.py — Command-to-intent parser for kubectl commands.

KubectlIntent fields
--------------------
verb             The kubectl sub-command: logs, top, get, describe, rollout, set, patch,
                 delete, scale, apply, exec, ...
                 "unknown" for unrecognised or unparseable input.
resource         Kubernetes resource type (pod, deployment, node, service, events, all)
                 OR the sub-verb for compound commands:
                   rollout restart  → resource = "restart"
                   rollout status   → resource = "status"
                   set resources    → resource = "resources"
                 None for single-verb commands (logs, exec, apply).
target           Specific resource name ("worker-api", "worker-api-7d9f8b-xk2q").
                 None when no specific target (list all resources).
namespace        Kubernetes namespace extracted from -n / --namespace; defaults to
                 "production" to match the LogPulse scenario convention.
flags            Parsed --flag=value / --flag value pairs.
                 Short flags decoded: -n → namespace, -o → output, -f → filename.
                 Namespace is consumed here and moved to the `namespace` field.
raw_args         Remaining positional tokens after verb/resource/target extraction.
original_command The raw command string, unmodified, for regex extraction when needed.

Resource aliases
----------------
All plural / shorthand forms are normalised to the canonical singular:
  pods/po → pod | deployments/deploy → deployment | nodes/no → node
  services/svc → service | events/ev → events

Usage
-----
    from ai_core.simulation.intent_parser import parse_kubectl, KubectlIntent

    intent = parse_kubectl("kubectl rollout restart deployment/worker-api")
    # intent.verb == "rollout", intent.resource == "restart", intent.target == "worker-api"

    intent = parse_kubectl("kubectl logs worker-api -n staging --tail=50")
    # intent.verb == "logs", intent.target == "worker-api"
    # intent.namespace == "staging", intent.flags == {"tail": 50}
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class KubectlIntent:
    verb:             str
    resource:         Optional[str]   = None
    target:           Optional[str]   = None
    namespace:        str             = "production"
    flags:            Dict[str, Any]  = field(default_factory=dict)
    raw_args:         List[str]       = field(default_factory=list)
    original_command: str             = ""


# ---------------------------------------------------------------------------
# Resource-type alias table
# ---------------------------------------------------------------------------

_RESOURCE_ALIASES: Dict[str, str] = {
    "pod": "pod",   "pods": "pod",   "po": "pod",
    "deployment": "deployment", "deployments": "deployment", "deploy": "deployment",
    "node": "node", "nodes": "node", "no": "node",
    "service": "service", "services": "service", "svc": "service",
    "event": "events", "events": "events", "ev": "events",
    "all": "all",
    "configmap": "configmap", "configmaps": "configmap", "cm": "configmap",
    "secret": "secret", "secrets": "secret",
    "namespace": "namespace", "namespaces": "namespace", "ns": "namespace",
}

# Short flags to long-form key mapping
_SHORT_FLAGS: Dict[str, str] = {
    "-n": "namespace",
    "-o": "output",
    "-f": "filename",
    "-c": "container",
    "-l": "selector",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_kubectl(command: str) -> KubectlIntent:
    """
    Parse *command* into a KubectlIntent.

    Accepts commands with or without the leading 'kubectl' token.
    Never raises — returns verb="unknown" on unparseable input.
    """
    raw  = command.strip()
    body = raw[len("kubectl"):].lstrip() if raw.lower().startswith("kubectl") else raw

    tokens = _safe_split(body)
    if not tokens:
        return KubectlIntent(verb="unknown", original_command=raw)

    flags, positionals = _extract_flags(tokens)
    namespace = flags.pop("namespace", "production")

    if not positionals:
        return KubectlIntent(verb="unknown", namespace=namespace, flags=flags, original_command=raw)

    verb = positionals[0].lower()
    rest = positionals[1:]

    # ── compound verbs: rollout <action>, set <action> ────────────────────
    if verb in ("rollout", "set") and rest:
        sub      = rest[0].lower()
        resource = sub
        rest     = rest[1:]
        target   = _extract_target(rest)
        return KubectlIntent(
            verb=verb, resource=resource, target=target,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── single-verb: logs ─────────────────────────────────────────────────
    if verb == "logs":
        target = _extract_target(rest)
        return KubectlIntent(
            verb=verb, resource=None, target=target,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── single-verb: exec ─────────────────────────────────────────────────
    if verb == "exec":
        target = _extract_target(rest)
        return KubectlIntent(
            verb=verb, resource=None, target=target,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── single-verb: apply ────────────────────────────────────────────────
    if verb == "apply":
        return KubectlIntent(
            verb=verb, resource=None, target=None,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── top pod|node [name] ───────────────────────────────────────────────
    if verb == "top":
        resource_token = rest[0].lower() if rest else "pod"
        resource       = _RESOURCE_ALIASES.get(resource_token, resource_token)
        target_rest    = rest[1:] if rest else []
        target         = _extract_target(target_rest)
        return KubectlIntent(
            verb=verb, resource=resource, target=target,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── resource-targeting verbs: get, describe, delete, patch, scale ─────
    if verb in ("get", "describe", "delete", "patch", "scale"):
        resource_token = rest[0] if rest else ""
        target_rest    = rest[1:]

        if "/" in resource_token:
            # Handle shorthand: pod/worker-api or deployment/worker-api
            rtype, rname = resource_token.split("/", 1)
            resource     = _RESOURCE_ALIASES.get(rtype.lower(), rtype.lower())
            target       = rname or None
        else:
            resource = _RESOURCE_ALIASES.get(resource_token.lower(), resource_token.lower() or None)
            target   = _extract_target(target_rest)

        return KubectlIntent(
            verb=verb, resource=resource, target=target or None,
            namespace=namespace, flags=flags,
            raw_args=rest, original_command=raw,
        )

    # ── unknown ───────────────────────────────────────────────────────────
    return KubectlIntent(
        verb="unknown", resource=None, target=None,
        namespace=namespace, flags=flags,
        raw_args=rest, original_command=raw,
    )


# Backward-compat alias used by command_router between sessions
parse_kubectl_intent = parse_kubectl


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _safe_split(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _extract_flags(tokens: List[str]) -> tuple[Dict[str, Any], List[str]]:
    """
    Walk *tokens* and separate flag tokens from positional tokens.

    Handles:
    - ``--key=value`` and ``--key value``
    - Known short flags (``-n``, ``-o``, ``-f``, ``-c``, ``-l``)
    - Unknown single-char flags — consume next token if it doesn't look like a flag
    - ``--`` separator: everything after it is positional
    """
    flags: Dict[str, Any] = {}
    positionals: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok == "--":
            positionals.extend(tokens[i + 1:])
            break

        if tok.startswith("--"):
            if "=" in tok:
                key, val = tok[2:].split("=", 1)
                flags[key.replace("-", "_")] = _coerce(val)
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                flags[tok[2:].replace("-", "_")] = _coerce(tokens[i + 1])
                i += 1
            else:
                flags[tok[2:].replace("-", "_")] = True

        elif tok in _SHORT_FLAGS:
            long_key = _SHORT_FLAGS[tok]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                flags[long_key] = _coerce(tokens[i + 1])
                i += 1
            else:
                flags[long_key] = True

        elif re.match(r"^-[a-zA-Z]$", tok):
            # Unknown single-char flag — consume its value if present
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1  # skip the value too

        else:
            positionals.append(tok)

        i += 1

    return flags, positionals


def _extract_target(tokens: List[str]) -> Optional[str]:
    """Return the first non-flag token, stripping any resource/ prefix."""
    for tok in tokens:
        if not tok.startswith("-"):
            return tok.split("/", 1)[-1]
    return None


def _coerce(val: str) -> Any:
    """Convert numeric strings to int, boolean strings to bool; otherwise str."""
    lv = val.lower()
    if lv in ("true", "yes"):
        return True
    if lv in ("false", "no"):
        return False
    try:
        return int(val)
    except ValueError:
        return val
