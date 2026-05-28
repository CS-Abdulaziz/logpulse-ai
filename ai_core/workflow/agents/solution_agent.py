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
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_AGENTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_CACHE_DIR    = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "cache"))
_EVENTS_DIR   = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "events"))
_PROJECT_ROOT = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", ".."))

for _p in (_WORKFLOW_DIR, _CACHE_DIR, _EVENTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import DiagnosticResult, LogState, RagResult, SolutionResult  # noqa: E402
from recorder import record   # noqa: E402
from models import EventType  # noqa: E402


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

MODEL_NAME  = "gemini-3.5-flash"
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


def _build_prompt(state: LogState) -> str:
    """
    Build the Gemini prompt from the real root-cause and available playbook.

    The prompt is deliberately incident-aware — it includes the actual root
    cause from diagnostic_agent so the LLM synthesises commands that address
    the *real* issue, not a generic PostgreSQL template.
    """
    diag: Optional[DiagnosticResult] = state.diagnostic_result
    rag: Optional[RagResult] = state.rag_result

    root_cause_block = (
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

    prompt = f"""\
You are an expert SRE remediation engineer. Given the incident details below,
produce a CONCRETE, EXECUTABLE remediation plan that directly addresses the
diagnosed root cause.

== RAW LOG ==
{state.raw_log}

== DIAGNOSED ROOT CAUSE ==
{root_cause_block}

== RETRIEVED PLAYBOOK (if any) ==
{playbook_block}

== TASK ==
Return ONLY a JSON object with these exact keys:
{{
  "steps": ["<step 1 description>", "<step 2 description>", ...],
  "commands": ["<command 1>", "<command 2>", ...],
  "explanation": "<one paragraph: why these steps fix the root cause>"
}}

Rules:
- steps and commands must be parallel arrays (same length, same order).
- commands must be real, executable shell/kubectl/systemctl/etc. commands.
- Do NOT produce PostgreSQL commands if the incident is Kubernetes OOMKilled.
- Tailor every command to the specific technology in the raw log.

CRITICAL: Do NOT converse. Do NOT add text outside the JSON block.
Your output must be ONLY the JSON object above — no markdown, no preamble.
"""
    return prompt


def _call_gemini(prompt: str) -> Dict[str, Any]:
    """
    Call Gemini and parse the JSON response.  Raises on any failure.
    """
    if not GEMINI_AVAILABLE or _genai_client is None:
        raise RuntimeError("Gemini client not initialised.")

    response = _genai_client.models.generate_content(
        model=_active_model,
        contents=prompt,
        config=_generate_config,
    )
    text: str = response.text.strip()

    # Robust fence removal (same multi-step approach as diagnostic_agent)
    text = re.sub(r"^```(?:json)?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"^```\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text.strip())
    text = text.strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in Gemini response: {text!r}")

    return json.loads(match.group(0))


def _parse_steps_from_playbook(playbook_text: str) -> tuple[List[str], List[str]]:
    """
    Extract human-readable steps and plausible commands from raw playbook text.

    Heuristic: lines starting with a bullet (•, -, *, digit+dot) are treated
    as steps.  Lines that look like shell commands (start with a known verb or
    contain '|', '&&', '$', 'kubectl', 'systemctl', 'sed', etc.) are captured
    as commands too.
    """
    steps: List[str] = []
    commands: List[str] = []

    _cmd_pattern = re.compile(
        r"^\s*(?:kubectl|systemctl|docker|helm|sed|awk|grep|kill|ps|"
        r"top|free|df|du|journalctl|dmesg|echo|export|set|unset|"
        r"patch|apply|delete|describe|get|logs|exec)\b",
        re.IGNORECASE,
    )
    _bullet_pattern = re.compile(r"^\s*(?:[•\-\*]|\d+\.)\s+(.+)")

    for line in playbook_text.splitlines():
        bullet_match = _bullet_pattern.match(line)
        if bullet_match:
            content = bullet_match.group(1).strip()
            steps.append(content)
            if _cmd_pattern.match(content):
                commands.append(content)

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

    return SolutionResult(
        steps=steps,
        commands=commands,
        explanation=(
            f"Gemini unavailable; remediation derived from playbook '{title}' "
            f"(RAG confidence={conf:.4f}). Review the playbook steps and adapt "
            f"commands to the live environment before execution."
        ),
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
        raw = _call_gemini(prompt)

        # Normalise steps/commands: accept list or comma-separated string
        raw_steps: List[str] = raw.get("steps") or []
        raw_cmds: List[str] = raw.get("commands") or []

        if isinstance(raw_steps, str):
            raw_steps = [s.strip() for s in raw_steps.split(",") if s.strip()]
        if isinstance(raw_cmds, str):
            raw_cmds = [c.strip() for c in raw_cmds.split(",") if c.strip()]

        result = SolutionResult(
            steps=[str(s) for s in raw_steps],
            commands=[str(c) for c in raw_cmds],
            explanation=raw.get("explanation", ""),
            source="llm",
        )

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
            sol = _playbook_fallback(state)
            record(
                incident_id=state.trace_id,
                event_type=EventType.SOLUTION_GENERATED,
                node_name="solution_agent_node",
                message=f"{len(sol.steps)} step(s) from playbook",
                metadata={"source": "playbook_fallback"},
            )
            return {"solution_result": sol}

        # ── Tier 3: safe fallback ──────────────────────────────────────────────────
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
