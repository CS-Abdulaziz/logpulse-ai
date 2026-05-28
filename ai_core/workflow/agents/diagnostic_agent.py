"""
diagnostic_agent.py — Gemini-powered RAG-augmented diagnostic node for LogPulse.

Architecture
------------
The node synthesizes all upstream evidence (raw log, classification, retrieved
playbook, incident history) into a structured root-cause diagnosis.  It does NOT
diagnose from scratch — the upstream nodes did the retrieval; this node confirms,
refines, and articulates the diagnosis using Gemini Flash as the synthesis engine.

Three-tier fallback (resilience)
---------------------------------
1. GEMINI AVAILABLE  →  full LLM synthesis           → source="llm"
2. Gemini fails BUT strong playbook (confidence ≥ 0.5) → source="playbook_fallback"
3. Both unavailable  →  generic safe message          → source="safe_fallback"

The node NEVER raises out of the node function.  Any exception at any tier
triggers the next fallback tier automatically.

API Key
-------
Set GEMINI_API_KEY in a .env file at the project root.  Never hardcode it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Path bootstrap — resolve imports regardless of working directory
# (pytest from project root, or python main.py from ai_core/workflow/).
# ---------------------------------------------------------------------------
_AGENTS_DIR   = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_DIR = os.path.normpath(os.path.join(_AGENTS_DIR, ".."))
_CACHE_DIR    = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "cache"))
_EVENTS_DIR   = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", "events"))
_PROJECT_ROOT = os.path.normpath(os.path.join(_AGENTS_DIR, "..", "..", ".."))

for _p in (_WORKFLOW_DIR, _CACHE_DIR, _EVENTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from state import DiagnosticResult, HistoryContext, LogState, RagResult  # noqa: E402
from recorder import record   # noqa: E402
from models import EventType  # noqa: E402


# ---------------------------------------------------------------------------
# Load .env (best-effort — if python-dotenv is not installed, read env as-is)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore
    _dotenv_path = os.path.join(_PROJECT_ROOT, ".env")
    load_dotenv(dotenv_path=_dotenv_path, override=False)
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables directly


# ---------------------------------------------------------------------------
# Gemini SDK initialisation
# ---------------------------------------------------------------------------

MODEL_NAME = "gemini-3.5-flash"
_FALLBACK_MODEL = "gemini-1.5-flash"

# Acceptance floor: a playbook must score ≥ this to serve as a fallback
# diagnosis when Gemini is unavailable.
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

        # Probe that the model name is valid by storing config; actual
        # availability is confirmed on the first real call.
        _active_model = MODEL_NAME
        _generate_config = genai_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
        )

        GEMINI_AVAILABLE = True
        print(f"[Diagnostic] Gemini client ready ({_active_model}).")
    else:
        print(
            "[Diagnostic] WARNING: GEMINI_API_KEY not set — "
            "diagnostic node will use playbook/safe fallback."
        )
except ImportError:
    print(
        "[Diagnostic] WARNING: google-genai not installed — "
        "diagnostic node will use playbook/safe fallback."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _build_prompt(state: LogState) -> str:
    """
    Construct the Gemini prompt from all available upstream evidence.

    Sections are included only when the corresponding evidence is present
    and trustworthy (e.g. playbook section is omitted when rag returned
    a safe fallback with zero confidence; history section marks first
    occurrences explicitly).
    """
    rag: Optional[RagResult] = state.rag_result
    hist: Optional[HistoryContext] = state.history_context

    # ── Classification block ───────────────────────────────────────────────
    if state.classification:
        cls = state.classification
        classification_block = (
            f"Category: {cls.category}   "
            f"Source: {cls.source}   "
            f"Severity: {cls.severity.value}\n"
            f"Summary: {cls.summary}"
        )
    else:
        classification_block = "(classification unavailable — diagnose from raw log only)"

    # ── Playbook block ─────────────────────────────────────────────────────
    rag_conf = (rag.rag_confidence or 0.0) if rag else 0.0
    has_playbook = rag is not None and rag.playbook_steps and rag_conf > 0.0
    if has_playbook:
        playbook_block = (
            f"{rag.playbook_steps}\n"
            f"Playbook confidence: {rag_conf:.4f}"
        )
    else:
        playbook_block = "none / low confidence (do not use playbook as evidence)"

    # ── History block ──────────────────────────────────────────────────────
    if hist is not None and not hist.is_first_occurrence:
        history_block = (
            f"This exact incident has occurred {hist.occurrence_count} time(s) before.\n"
            f"Previous root cause: {hist.previous_root_cause or 'not recorded'}\n"
            f"Previous solution  : {hist.previous_solution or 'not recorded'} "
            f"(outcome: {hist.previous_outcome or 'unknown'})\n"
            "Strongly consider whether the same root cause applies again."
        )
    else:
        history_block = "First occurrence — no prior history on record."

    prompt = f"""\
You are an expert SRE diagnostic assistant. Given the evidence below about a
production incident, determine the single most likely ROOT CAUSE.

You are NOT diagnosing from scratch — synthesize the evidence provided.

== RAW LOG ==
{state.raw_log}

== CLASSIFICATION ==
{classification_block}

== RETRIEVED PLAYBOOK (if any) ==
{playbook_block}

== INCIDENT HISTORY ==
{history_block}

== TASK ==
Return ONLY a JSON object — no markdown fences, no extra text:
{{
  "root_cause": "<one or two sentences, specific and actionable>",
  "confidence": <0.0-1.0, float>,
  "reasoning": "<short: which evidence drove the conclusion>"
}}

CRITICAL: Do NOT converse. Do NOT say 'But wait' or analyze previous mocks.
Look ONLY at the current LogState input evidence shown above.
Your output must contain absolutely ZERO text outside the JSON block.
"""
    return prompt


def _call_gemini(prompt: str) -> Dict[str, Any]:
    """
    Send *prompt* to the configured Gemini model and parse the JSON response.

    Raises on any failure so the caller's try/except routes to the next tier.
    """
    if not GEMINI_AVAILABLE or _genai_client is None:
        raise RuntimeError("Gemini client not initialised.")

    response = _genai_client.models.generate_content(
        model=_active_model,
        contents=prompt,
        config=_generate_config,
    )
    text: str = response.text.strip()

    # ------------------------------------------------------------------
    # Robust Markdown fence removal.
    #
    # Gemini Flash sometimes wraps its JSON in code fences despite the
    # explicit "no markdown" instruction in the prompt.  Handle every
    # known variant defensively:
    #
    #   ```json          (opening fence, with/without language tag, own line)
    #   { ... }
    #   ```              (closing fence, own line)
    #
    # Also handle the inline variant:  ```json{ ... }```  (no newline).
    #
    # MULTILINE flag lets ^ / $ match at the start/end of each line so
    # the fence substitution fires even when the fence is not at the very
    # start of the whole string.
    # ------------------------------------------------------------------

    # Step 1: remove an opening fence line (```json or ```, alone on its line)
    text = re.sub(r"^```(?:json)?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    # Step 2: remove a closing fence line (``` alone on its line)
    text = re.sub(r"^```\s*$", "", text, flags=re.MULTILINE)
    # Step 3: handle inline variant — fences not separated by a newline
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text.strip())
    text = text.strip()

    # Step 4: extract the first {...} block — guards against any residual text.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in Gemini response: {text!r}")

    parsed: Dict[str, Any] = json.loads(match.group(0))
    return parsed


def _playbook_fallback(state: LogState) -> DiagnosticResult:
    """
    Build a degraded diagnosis from the retrieved playbook when Gemini is
    unavailable but the RAG confidence is strong enough to be informative.
    """
    rag: RagResult = state.rag_result  # type: ignore[assignment]  # caller guarantees non-None
    conf = rag.rag_confidence or 0.0

    # Extract the playbook title from the first line if present.
    first_line = (rag.playbook_steps or "").split("\n")[0].strip()
    title_match = re.match(r"\[Playbook:\s*(.+?)\]", first_line)
    title = title_match.group(1) if title_match else "retrieved playbook"

    root_cause = (
        f"Based on the matched playbook '{title}', the likely root cause is a "
        f"{state.classification.category if state.classification else 'system'} "
        f"incident matching the pattern: {state.classification.summary if state.classification else state.raw_log[:120]}."
    )

    return DiagnosticResult(
        root_cause=root_cause,
        confidence=_clamp(conf, 0.0, 0.6),   # cap at 0.6 — no LLM reasoning
        reasoning=(
            f"Gemini unavailable; diagnosis derived from playbook '{title}' "
            f"(RAG confidence={conf:.4f})."
        ),
        used_history=False,
        used_playbook=True,
        source="playbook_fallback",
    )


def _safe_fallback(reason: str) -> DiagnosticResult:
    """
    Return an explicit low-confidence fallback when both Gemini and the
    playbook are unavailable or unreliable.
    """
    return DiagnosticResult(
        root_cause=(
            "Unable to determine root cause automatically — "
            "manual investigation required."
        ),
        confidence=0.1,
        reasoning=f"All diagnosis paths unavailable: {reason}",
        used_history=False,
        used_playbook=False,
        source="safe_fallback",
    )


# ---------------------------------------------------------------------------
# LangGraph node function
# ---------------------------------------------------------------------------

def diagnostic_agent_node(state: LogState) -> Dict[str, Any]:
    """
    LangGraph node — RAG-augmented root-cause diagnosis via Gemini Flash.

    Reads: raw_log, classification, rag_result, history_context
    Writes: diagnostic_result (DiagnosticResult)

    Behaviour
    ---------
    Tier 1 — Gemini available:
        Builds a structured prompt from all upstream evidence and calls Gemini.
        Parses the JSON response into a DiagnosticResult with source="llm".

    Tier 2 — Gemini fails + strong playbook (rag_confidence ≥ threshold):
        Builds a text diagnosis from the playbook title/steps.
        source="playbook_fallback"

    Tier 3 — Both unavailable:
        Returns a generic "manual investigation required" message.
        source="safe_fallback"

    The node NEVER raises.  Any uncaught exception triggers the next tier.
    """
    rag: Optional[RagResult] = state.rag_result
    hist: Optional[HistoryContext] = state.history_context

    rag_conf = (rag.rag_confidence or 0.0) if rag else 0.0
    has_strong_playbook = rag is not None and rag.playbook_steps and rag_conf >= PLAYBOOK_FALLBACK_THRESHOLD
    has_history = hist is not None and not hist.is_first_occurrence

    try:
        if not GEMINI_AVAILABLE:
            raise RuntimeError("Gemini not configured — skipping to fallback.")

        prompt = _build_prompt(state)
        raw = _call_gemini(prompt)

        result = DiagnosticResult(
            root_cause=raw["root_cause"],
            confidence=_clamp(float(raw["confidence"]), 0.0, 1.0),
            reasoning=raw.get("reasoning", ""),
            used_history=has_history,
            used_playbook=rag_conf >= PLAYBOOK_FALLBACK_THRESHOLD,
            source="llm",
        )

        print(
            f"[Diagnostic] root cause via LLM "
            f"(confidence={result.confidence:.2f}, "
            f"used_history={result.used_history}, "
            f"used_playbook={result.used_playbook})"
        )
        record(
            incident_id=state.trace_id,
            event_type=EventType.ROOT_CAUSE_GENERATED,
            node_name="diagnostic_agent_node",
            message=result.root_cause[:120],
            metadata={"source": "llm", "confidence": result.confidence},
        )
        return {"diagnostic_result": result}

    except Exception as exc:
        # ── Tier 2: playbook fallback ──────────────────────────────────────────────────
        if has_strong_playbook:
            print(
                f"[Diagnostic] Gemini unavailable — using playbook fallback. "
                f"({exc})"
            )
            result = _playbook_fallback(state)
            record(
                incident_id=state.trace_id,
                event_type=EventType.ROOT_CAUSE_GENERATED,
                node_name="diagnostic_agent_node",
                message=result.root_cause[:120],
                metadata={"source": "playbook_fallback"},
            )
            return {"diagnostic_result": result}

        # ── Tier 3: safe fallback ──────────────────────────────────────────────────
        print(f"[Diagnostic] No reliable evidence — safe fallback. ({exc})")
        result = _safe_fallback(str(exc))
        record(
            incident_id=state.trace_id,
            event_type=EventType.ROOT_CAUSE_GENERATED,
            node_name="diagnostic_agent_node",
            message=result.root_cause[:120],
            metadata={"source": "safe_fallback"},
        )
        return {"diagnostic_result": result}
