"""
solution_agent.py — Gemini-powered remediation plan node for LogPulse.

Architecture
------------
Takes the real root-cause diagnosis from diagnostic_agent_node and synthesises
a concrete, incident-specific remediation plan.  The plan is expressed as:

  • steps    — human-readable action descriptions (one per bullet)
  • commands — executable CLI / Bash / Kubectl strings
  • explanation — one-paragraph justification tying the plan to the root cause

The node reads:
  state.raw_log              — original log for context
  state.diagnostic_result    — root_cause, confidence, source from the prior node
  state.rag_result           — playbook_steps, rag_confidence

Three-tier fallback (mirrors diagnostic_agent)
------------------------------------------------
1. GEMINI AVAILABLE    → full LLM synthesis                  → source="llm"
2. Gemini fails + playbook (rag_confidence ≥ 0.5)           → source="playbook_fallback"
3. Both unavailable    → generic safe SRE triage steps       → source="safe_fallback"

The node NEVER raises out of the node function.
API key: set GEMINI_API_KEY in .env at the project root.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from ai_core.workflow.state import DiagnosticResult, LogState, RagResult, SolutionResult
from ai_core.events.recorder import record
from ai_core.events.models import EventType
from ai_core.workflow.agents.context_resolver import extract_context, resolve_placeholders

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"), override=False)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Gemini SDK initialisation (shares the same API key as diagnostic_agent)
# ---------------------------------------------------------------------------

MODEL_NAME  = "gemini-2.5-flash"
PLAYBOOK_FALLBACK_THRESHOLD: float = 0.5

GEMINI_AVAILABLE: bool = False
_genai_client = None    # google.genai.Client | None
_active_model: str = MODEL_NAME
_generate_config = None

try:
    import google.genai as genai          # type: ignore
    import google.genai.types as genai_types  # type: ignore

    _api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if _api_key:
        _genai_client = genai.Client(api_key=_api_key)
        _active_model = MODEL_NAME
        _generate_config = genai_types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=2048,
        )
        GEMINI_AVAILABLE = True
        print(f"[Solution] Gemini client ready ({_active_model}).")
    else:
        print(
            "[Solution] WARNING: GEMINI_API_KEY not set — "
            "solution node will use playbook/safe fallback."
        )
except ImportError:
    print(
        "[Solution] WARNING: google-genai not installed — "
        "solution node will use playbook/safe fallback."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _build_cluster_context_block(state: LogState) -> tuple[str, "IncidentContext"]:
    """
    Build a human-readable cluster-context block for prompt injection and
    return the parsed IncidentContext for default-value substitution.

    Importing here (not at module level) keeps the circular-import risk at
    zero; context_resolver has no dependency on solution_agent.
    """
    from ai_core.workflow.agents.context_resolver import extract_context, IncidentContext
    ctx: IncidentContext = extract_context(state)

    lines: List[str] = []
    if ctx.pod_name:
        lines.append(f"  pod_name        : {ctx.pod_name}")
    if ctx.deployment_name:
        lines.append(f"  deployment_name : {ctx.deployment_name}")
    if ctx.container_name and ctx.container_name != ctx.deployment_name:
        lines.append(f"  container_name  : {ctx.container_name}")
    if ctx.namespace:
        lines.append(f"  namespace       : {ctx.namespace}")
    if ctx.node_name:
        lines.append(f"  node_name       : {ctx.node_name}")
    if ctx.service_name and ctx.service_name != ctx.deployment_name:
        lines.append(f"  service_name    : {ctx.service_name}")
    if ctx.attacker_ip:
        lines.append(f"  attacker_ip     : {ctx.attacker_ip}")
    if ctx.locked_account:
        lines.append(f"  locked_account  : {ctx.locked_account}")
    if ctx.memory_limit:
        lines.append(f"  memory_limit    : {ctx.memory_limit}")

    block = "\n".join(lines) if lines else "  (no structured cluster context — infer from log)"
    return block, ctx


def _build_prompt(state: LogState) -> str:
    """
    Build the Gemini prompt for strict, executable command synthesis.

    The prompt enforces the production incident-response engineer contract:
    - Every command must be a real, executable CLI string (no prose).
    - Placeholders must be resolved using the injected cluster context.
    - Output format is ``{root_cause, commands: [{cmd, risk, description}]}``.

    Cluster context is injected directly so the LLM never needs to guess
    entity names (pod, deployment, namespace, attacker IP, etc.).
    """
    diag: Optional[DiagnosticResult] = state.diagnostic_result
    rag: Optional[RagResult] = state.rag_result

    root_cause_text = (
        diag.root_cause if diag else "(root cause unavailable — infer from raw log)"
    )

    rag_conf = (rag.rag_confidence or 0.0) if rag else 0.0
    if rag and rag.playbook_steps and rag_conf > 0.0:
        playbook_block = (
            f"{rag.playbook_steps}\n"
            f"Playbook confidence: {rag_conf:.4f}"
        )
    else:
        playbook_block = "none / low confidence"

    cluster_block, ctx = _build_cluster_context_block(state)

    # Safe defaults for inline f-string substitutions in the prompt examples
    _dep   = ctx.deployment_name or "worker-api"
    _ns    = ctx.namespace        or "default"

    prompt = f"""\
You are a production-grade incident response engineer inside LogPulse AI.
Your job is to generate STRICTLY VALID, EXECUTABLE remediation commands.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INCIDENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RAW LOG:
{state.raw_log}

ROOT CAUSE:
{root_cause_text}

CLUSTER CONTEXT — use these exact values, NEVER use angle-bracket placeholders:
{cluster_block}

RETRIEVED PLAYBOOK:
{playbook_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT RULES (NON-NEGOTIABLE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY this JSON — no markdown, no preamble, no extra text:

{{
  "root_cause": "one-sentence summary of the root cause",
  "commands": [
    {{
      "cmd": "<exact executable command>",
      "risk": "safe | warning | dangerous",
      "description": "short explanation of what this command does"
    }}
  ]
}}

COMMAND RULES:
1. NEVER output prose as a command.
   ❌  Analyze memory usage using monitoring tools
   ✔   kubectl top pod {_dep}

2. NEVER use angle-bracket placeholders such as <pod-name> or <deployment-name>.
   ❌  kubectl rollout restart deployment/<deployment-name>
   ✔   kubectl rollout restart deployment/{_dep}
   All real names are in CLUSTER CONTEXT above — use them verbatim.

3. ONLY produce real, executable CLI commands from:
     kubectl | ssh | dmesg | systemctl | docker | ps | top | free
     iptables | faillock | journalctl | curl (health checks only)

4. If a playbook step cannot be expressed as an executable command → omit it.

5. Namespace: use "{_ns}" unless the incident specifies a different one.

6. Risk classification per command:
     safe      = read-only, zero side-effects
     warning   = impactful but reversible (restart, patch, scale, block IP)
     dangerous = irreversible or potentially disruptive (delete, drain, format)

If no valid commands can be generated:
{{"root_cause": "...", "commands": []}}
"""
    return prompt


def _extract_and_parse_json(text: str) -> Dict[str, Any]:
    """
    Extract and parse a JSON object from a raw string.

    Extraction order
    ----------------
    1. Direct parse — no fences, no preamble.
    2. Markdown code fence — ``\\`\\`\\`json\\n{...}\\n\\`\\`\\`\\`` or bare
       ``\\`\\`\\`\\n{...}\\n\\`\\`\\`\\``.  The non-greedy ``.*?`` in the capture
       group expands until the last ``}`` that is immediately followed by
       ``\\s*\\`\\`\\``\\``, which is always the outermost JSON closing brace.
    3. First ``{`` to last ``}`` — handles leading/trailing prose outside a
       fence block without requiring the fence markers.
    4. Exhaustive start/end combination scan — last resort for deeply
       embedded JSON with extra braces in surrounding text.
    """
    cleaned = text.strip()

    # Step 1: direct parse (handles plain JSON responses)
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        pass

    # Step 2: explicit markdown code-fence extraction
    # Covers: ```json\n{...}\n```  and  ```\n{...}\n```
    # re.DOTALL lets . match newlines inside the JSON object.
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        cleaned,
        re.DOTALL,
    )
    if fence_match:
        try:
            return json.loads(fence_match.group(1), strict=False)
        except json.JSONDecodeError:
            pass  # malformed content inside fence — fall through

    # Step 3: first { … last } (preamble / postamble outside a fence block)
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1], strict=False)
        except json.JSONDecodeError:
            pass

    # Step 4: exhaustive multi-start scan (deeply embedded JSON with stray braces)
    start_indices = [m.start() for m in re.finditer(r"\{", cleaned)]
    end_indices   = [m.start() for m in re.finditer(r"\}", cleaned)]
    for si in start_indices:
        for ei in reversed(end_indices):
            if si < ei:
                try:
                    return json.loads(cleaned[si : ei + 1], strict=False)
                except json.JSONDecodeError:
                    continue

    raise ValueError(f"No JSON object found in Gemini response: {text!r}")


def _parse_llm_response(raw: Dict[str, Any] | str) -> tuple[List[str], List[str], str]:
    """
    Parse the Gemini response dict or raw string into ``(steps, commands, explanation)``.

    Supports the current structured format::

        {
          "root_cause": "...",
          "commands": [
            {"cmd": "...", "risk": "...", "description": "..."},
            ...
          ]
        }

    And falls back to the legacy flat format::

        {
          "steps":    ["...", ...],
          "commands": ["...", ...],
          "explanation": "..."
        }

    Both formats may optionally have ``commands`` as a comma-separated string
    (Gemini occasionally forgets arrays) which is split automatically.

    Returns
    -------
    steps       : human-readable descriptions for each remediation step
    commands    : executable CLI command strings (empty / invalid entries dropped)
    explanation : single-paragraph summary / root cause
    """
    if isinstance(raw, str):
        parsed = _extract_and_parse_json(raw)
    else:
        parsed = raw

    raw_cmds = parsed.get("commands") or []

    # ── New structured format: commands is a list of objects ─────────────
    if (
        isinstance(raw_cmds, list)
        and raw_cmds
        and isinstance(raw_cmds[0], dict)
    ):
        steps    = [
            str(c.get("description", "")).strip()
            for c in raw_cmds
            if isinstance(c, dict) and c.get("description", "").strip()
        ]
        commands = [
            str(c.get("cmd", "")).strip()
            for c in raw_cmds
            if isinstance(c, dict) and str(c.get("cmd", "")).strip()
        ]
        explanation = str(parsed.get("root_cause", "")).strip()
        return steps, commands, explanation

    # ── Legacy flat format: steps/commands are plain lists or strings ─────
    raw_steps = parsed.get("steps") or []

    if isinstance(raw_steps, str):
        raw_steps = [s.strip() for s in raw_steps.split(",") if s.strip()]
    if isinstance(raw_cmds, str):
        raw_cmds  = [c.strip() for c in raw_cmds.split(",") if c.strip()]

    steps       = [str(s) for s in raw_steps]
    commands    = [str(c) for c in raw_cmds if str(c).strip()]
    explanation = str(parsed.get("explanation", "")).strip()
    return steps, commands, explanation


GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


def _call_gemini(prompt: str) -> str:
    """
    Try each model in GEMINI_MODELS order with up to 2 attempts per model.
    Waits 3 s before retrying a 503/429 on the same model, then advances
    to the next model.  Returns the raw text response on first success.
    Raises the last exception if all models and retries fail.
    """
    if not GEMINI_AVAILABLE or _genai_client is None:
        raise RuntimeError("Gemini client not initialised.")

    last_error: Optional[Exception] = None

    for model_name in GEMINI_MODELS:
        for attempt in range(2):
            try:
                response = _genai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=_generate_config,
                )
                raw_response = response.text
                if os.getenv("LOGPULSE_DEBUG") == "1":
                    print("\n===== RAW LLM RESPONSE =====")
                    print(raw_response)
                    print("===== END RESPONSE =====\n")
                return raw_response

            except Exception as e:
                last_error = e
                is_503 = "503" in str(e) or "UNAVAILABLE" in str(e)
                is_429 = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)

                if (is_503 or is_429) and attempt == 0:
                    time.sleep(3)
                    continue
                elif is_503 or is_429:
                    break
                else:
                    break  # unknown error → try next model

    raise last_error


def _parse_steps_from_playbook(playbook_text: str) -> tuple[List[str], List[str]]:
    """
    Extract human-readable steps and executable commands from raw playbook text.

    Three extraction strategies are applied in order:

    1. **Bullet-as-command** — bullet content that directly starts with a
       recognised command keyword (original behaviour, preserved).

    2. **Backtick-embedded commands** — commands written inside backtick
       notation within bullet prose, e.g.:
           "Roll out using `kubectl rollout restart deployment/foo`."
       Each backtick segment is tested against _cmd_pattern independently.

    3. **Standalone command lines** — non-bullet lines whose content starts
       with a command keyword; typically found in ``Code fix:`` sections, e.g.:
           kubectl set resources deployment/my-svc --limits=memory=2Gi
       Lines that end with ``:`` (YAML/section headers) and comment lines
       (``#``, ``//``) are skipped.

    Supported command prefixes cover kubectl, systemctl, docker, hdfs, hadoop,
    yarn, iptables / nftables / ufw, common shell utilities, and cloud CLIs
    (aws, az, gcloud).  Extend _cmd_pattern to add future tooling.

    Commands are deduplicated: the same string is never added twice.
    """
    steps: List[str] = []
    commands: List[str] = []
    _seen: set[str] = set()

    # Recognised command-verb prefixes — restricted to ACTUAL EXECUTABLE NAMES.
    #
    # Deliberately excluded (all are kubectl sub-commands, not Unix binaries):
    #   apply, delete, describe, exec, get, logs, patch
    # These words also start common English sentences ("Apply the manifest…",
    # "Get the metrics…", "Delete the old pods…") and would leak narrative
    # text into the commands list.  Full kubectl commands are captured via the
    # 'kubectl' entry at the top; kubectl sub-commands must always be preceded
    # by 'kubectl'.
    _cmd_pattern = re.compile(
        r"^\s*(?:"
        # Kubernetes tooling
        r"kubectl|helm|"
        # Containers / orchestration
        r"docker|podman|crictl|"
        # Linux service managers
        r"systemctl|service|"
        # HDFS / Hadoop ecosystem
        r"hdfs|hadoop|yarn|mapred|"
        # Network / firewall
        r"iptables|ip6tables|nftables|nft|ufw|firewall-cmd|"
        # User / account management (security playbooks)
        r"faillock|fail2ban-client|useradd|userdel|usermod|passwd|chage|"
        r"id|groups|last|who|w|pam_tally2|"
        # Shell utilities (unambiguous binaries only)
        r"sed|awk|grep|kill|ps|top|free|df|du|"
        r"journalctl|dmesg|echo|export|unset|"
        r"chmod|chown|mv|cp|tar|curl|wget|ssh|scp|rsync|"
        # Package managers
        r"apt(?:-get)?|yum|dnf|pip3?|npm|make|"
        # Version control / CI
        r"git|"
        # Cloud CLIs
        r"aws|az|gcloud|gsutil|kubectl-node-shell"
        r")\b",
        re.IGNORECASE,
    )
    _bullet_pattern = re.compile(r"^\s*(?:[•\-\*]|\d+\.)\s+(.+)")
    _backtick_pattern = re.compile(r"`([^`\n]+)`")

    def _try_add(candidate: str) -> None:
        """Validate and deduplicate a command candidate."""
        candidate = candidate.strip()
        if (
            candidate
            and not candidate.startswith("#")
            and not candidate.startswith("//")
            and candidate not in _seen
            and _cmd_pattern.match(candidate)
        ):
            _seen.add(candidate)
            commands.append(candidate)

    for line in playbook_text.splitlines():
        bullet_match = _bullet_pattern.match(line)
        if bullet_match:
            content = bullet_match.group(1).strip()
            steps.append(content)
            # Strategy 1: step text itself is a command
            _try_add(content)
            # Strategy 2: backtick-quoted commands embedded in prose
            for bt in _backtick_pattern.finditer(content):
                _try_add(bt.group(1))
        else:
            # Strategy 3: standalone command lines (code_fix / fenced sections)
            stripped = line.strip()
            if stripped and not stripped.endswith(":"):
                _try_add(stripped)
                # Also catch backtick-embedded commands in non-bullet lines
                for bt in _backtick_pattern.finditer(stripped):
                    _try_add(bt.group(1))

    return steps, commands


def _playbook_fallback(state: LogState) -> SolutionResult:
    """Build a degraded remediation plan from the retrieved playbook."""
    rag: RagResult = state.rag_result  # type: ignore[assignment]
    conf = rag.rag_confidence or 0.0

    first_line = (rag.playbook_steps or "").split("\n")[0].strip()
    title_match = re.match(r"\[Playbook:\s*(.+?)\]", first_line)
    title = title_match.group(1) if title_match else "retrieved playbook"

    steps, commands = _parse_steps_from_playbook(rag.playbook_steps or "")

    if not steps:
        steps = [f"Follow the '{title}' runbook to resolve this incident."]

    if commands:
        explanation = (
            f"Gemini unavailable; remediation derived from playbook '{title}' "
            f"(RAG confidence={conf:.4f}). Review the playbook steps and adapt "
            f"commands to the live environment before execution."
        )
    else:
        explanation = (
            f"Gemini unavailable; playbook '{title}' was retrieved "
            f"(RAG confidence={conf:.4f}) but contained no executable commands — "
            f"only narrative guidance was found. Review the playbook manually "
            f"and construct the appropriate commands before execution."
        )

    return SolutionResult(
        steps=steps,
        commands=commands,
        explanation=explanation,
        source="playbook_fallback",
    )


def _safe_fallback(reason: str) -> SolutionResult:
    """Return generic SRE triage steps when all other paths are unavailable."""
    return SolutionResult(
        steps=[
            "Identify and isolate the affected service or pod.",
            "Collect recent logs and metrics for the incident window.",
            "Check recent deployments or config changes that may have caused the issue.",
            "Escalate to the on-call engineer with collected evidence.",
            "Apply a rollback or hotfix once root cause is confirmed.",
        ],
        commands=[],   # safe_fallback never emits commands — requires human judgment
        explanation=(
            "All automated remediation paths were unavailable. "
            "The steps above represent generic SRE triage protocol. "
            f"Reason: {reason}"
        ),
        source="safe_fallback",
    )


# ---------------------------------------------------------------------------
# Placeholder resolution helper
# ---------------------------------------------------------------------------

def _apply_resolution(result: SolutionResult, state: LogState) -> SolutionResult:
    """
    Post-process a SolutionResult by substituting ``<placeholder>`` tokens
    in its commands with context-extracted entity values.

    Returns a *new* SolutionResult with resolved commands when at least one
    substitution was made; returns the original object otherwise.

    This is a best-effort step — any failure is caught and the original
    result is returned unchanged so the pipeline never breaks.
    """
    if not result.commands:
        return result
    try:
        ctx      = extract_context(state)
        resolved = resolve_placeholders(result.commands, ctx)
        n_fixed  = sum(1 for o, n in zip(result.commands, resolved) if o != n)
        if n_fixed > 0:
            print(f"[Solution] Resolved {n_fixed} placeholder(s) in commands.")
            return SolutionResult(
                steps=result.steps,
                commands=resolved,
                explanation=result.explanation,
                source=result.source,
            )
    except Exception as exc:
        print(f"[Solution] Placeholder resolution skipped ({exc}).")
    return result


# ---------------------------------------------------------------------------
# LangGraph node function
# ---------------------------------------------------------------------------

def solution_agent_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — Gemini-powered remediation plan synthesis.

    Reads: raw_log, diagnostic_result, rag_result
    Writes: solution_result (SolutionResult)

    Three-tier fallback:
      Tier 1 — Gemini success             → source="llm"
      Tier 2 — Gemini fails + playbook    → source="playbook_fallback"
      Tier 3 — Both unavailable           → source="safe_fallback"

    The node NEVER raises.
    """
    rag: Optional[RagResult] = state.rag_result
    rag_conf = (rag.rag_confidence or 0.0) if rag else 0.0
    has_strong_playbook = (
        rag is not None
        and rag.playbook_steps
        and rag_conf >= PLAYBOOK_FALLBACK_THRESHOLD
    )

    try:
        if not GEMINI_AVAILABLE:
            raise RuntimeError("Gemini not configured — skipping to fallback.")

        prompt = _build_prompt(state)
        raw    = _call_gemini(prompt)

        # Parse response — handles both new structured and legacy flat formats
        raw_steps, raw_cmds, explanation = _parse_llm_response(raw)

        result = SolutionResult(
            steps=raw_steps,
            commands=raw_cmds,
            explanation=explanation,
            source="llm",
        )

        # Resolve any <placeholder> tokens left by the LLM
        result = _apply_resolution(result, state)

        print(
            f"[Solution] plan via LLM "
            f"({len(result.steps)} step(s), {len(result.commands)} command(s))."
        )
        record(
            incident_id=state.trace_id,
            event_type=EventType.SOLUTION_GENERATED,
            node_name="solution_agent_node",
            message=f"{len(result.steps)} step(s), {len(result.commands)} command(s)",
            metadata={"source": "llm", "step_count": len(result.steps)},
        )
        return {"solution_result": result}

    except Exception as exc:
        # ── Tier 2: playbook fallback ──────────────────────────────────────────────────
        if has_strong_playbook:
            print(
                f"[Solution] Gemini unavailable — using playbook fallback. ({exc})"
            )
            sol = _apply_resolution(_playbook_fallback(state), state)
            record(
                incident_id=state.trace_id,
                event_type=EventType.SOLUTION_GENERATED,
                node_name="solution_agent_node",
                message=f"{len(sol.steps)} step(s) from playbook",
                metadata={"source": "playbook_fallback"},
            )
            return {"solution_result": sol}

        # ── Tier 3: safe fallback — commands=[] so resolution is a no-op ─────────
        print(f"[Solution] No reliable evidence — safe fallback. ({exc})")
        sol = _safe_fallback(str(exc))
        record(
            incident_id=state.trace_id,
            event_type=EventType.SOLUTION_GENERATED,
            node_name="solution_agent_node",
            message="safe fallback — manual investigation required",
            metadata={"source": "safe_fallback"},
        )
        return {"solution_result": sol}
