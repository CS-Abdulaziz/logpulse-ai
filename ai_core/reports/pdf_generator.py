"""ai_core/reports/pdf_generator.py

Generates a professional PDF incident report from a completed LogState.
Output: reports/incident_{trace_id[:8]}_{YYYY-MM-DD}.pdf
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

_HERE = Path(__file__).parent
from ai_core.events.recorder import build_incident_timeline

_PROJECT_ROOT = _HERE.parent.parent
_REPORTS_DIR = _PROJECT_ROOT / "reports"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
_DARK_GRAY = (55, 55, 55)
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
_MID_GRAY = (120, 120, 120)
_LIGHT_GRAY_BG = (245, 245, 245)
_GREEN = (34, 139, 34)
_AMBER = (200, 130, 0)
_RED = (180, 0, 0)

# ---------------------------------------------------------------------------
# Unicode → Latin-1 mapping for core font compatibility
# ---------------------------------------------------------------------------
_UNICODE_REPLACEMENTS = {
    "—": "--",   # em dash  —
    "–": "-",    # en dash  –
    "→": "->",   # arrow    →
    "←": "<-",   # arrow    ←
    "✓": "[OK]", # check    ✓
    "⚠": "[!]",  # warning  ⚠
    "•": "*",    # bullet   •
    "’": "'",    # rsquote  '
    "‘": "'",    # lsquote  '
    "“": '"',    # ldquote  "
    "”": '"',    # rdquote  "
    "…": "...",  # ellipsis …
    "°": "deg",  # degree   °
}


def _sanitize(text: str) -> str:
    """Replace non-Latin-1 chars so fpdf2 core fonts never raise."""
    for char, replacement in _UNICODE_REPLACEMENTS.items():
        text = text.replace(char, replacement)
    return text.encode("latin-1", errors="replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

def _safe(value: Any, fallback: str = "N/A", max_len: int = 500) -> str:
    """Return a string for *value*, handling None and truncating long text."""
    if value is None:
        return fallback
    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "... [truncated]"
    return s


def _s(value: Any, fallback: str = "N/A", max_len: int = 500) -> str:
    """Like _safe(), but also sanitizes for Latin-1 / core font compatibility."""
    return _sanitize(_safe(value, fallback, max_len))


def _safe_list(value: Any) -> List:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_report(state: Any) -> Optional[str]:
    """
    Generate a PDF report from *state* (a LogState object).

    Returns the output file path on success, or None if fpdf2 is
    unavailable or an unexpected error occurs — never crashes the pipeline.
    """
    try:
        from fpdf import FPDF  # noqa: F401
    except ImportError:
        logger.warning("[PDF] fpdf2 not installed -- skipping report")
        return None

    try:
        return _build_report(state)
    except Exception as exc:
        logger.error("[PDF] Report generation failed: %s", exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def _build_report(state: Any) -> str:
    from fpdf import FPDF

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    trace_id = _safe(getattr(state, "trace_id", None), "unknown")
    trace_short = trace_id[:8]
    filename = f"incident_{trace_short}_{date_str}.pdf"
    output_path = _REPORTS_DIR / filename

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    class ReportPDF(FPDF):
        def header(self_pdf):  # noqa: N805
            if self_pdf.page_no() > 1:
                self_pdf.set_font("Helvetica", size=8)
                self_pdf.set_text_color(*_MID_GRAY)
                self_pdf.cell(
                    0, 6,
                    _sanitize(f"LogPulse AI - Incident Report | {trace_id}"),
                    border=0,
                    new_x="LMARGIN", new_y="NEXT",
                )
                self_pdf.set_text_color(*_BLACK)

        def footer(self_pdf):  # noqa: N805
            self_pdf.set_y(-13)
            self_pdf.set_font("Helvetica", size=9)
            self_pdf.set_text_color(*_MID_GRAY)
            self_pdf.cell(0, 10, f"Page {self_pdf.page_no()}", border=0, align="C")
            self_pdf.set_text_color(*_BLACK)

    pdf = ReportPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    _doc_header(pdf, trace_id, generated_at)
    _section_1(pdf, state)
    _section_2(pdf, state)
    _section_3(pdf, state)
    _section_4(pdf, state)
    _section_5(pdf, state)
    _section_6(pdf, state)
    _section_7(pdf, state)
    _section_8(pdf, state)
    _section_9(pdf, state)

    pdf.output(str(output_path))
    return str(output_path)


# ---------------------------------------------------------------------------
# Layout primitives
# ---------------------------------------------------------------------------

def _section_head(pdf: Any, title: str) -> None:
    pdf.ln(4)
    pdf.set_fill_color(*_DARK_GRAY)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 9, f"  {_sanitize(title)}", border=0, fill=True,
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_BLACK)
    pdf.ln(3)


def _kv(pdf: Any, label: str, value: str, label_w: int = 44) -> None:
    """Render a bold-label / regular-value row that wraps long values."""
    pdf.set_font("Helvetica", style="B", size=10)
    # No new_x/new_y on cell → fpdf2 legacy mode: cursor advances by cell width
    pdf.cell(label_w, 6, _sanitize(f"{label} :"), border=0)
    pdf.set_font("Helvetica", size=10)
    val_w = pdf.w - pdf.r_margin - pdf.get_x()
    # Explicit new_x/new_y required: without them, legacy mode leaves x at right margin
    pdf.multi_cell(val_w, 6, _sanitize(value), border=0, align="L",
                   new_x="LMARGIN", new_y="NEXT")


def _mono_block(pdf: Any, text: str) -> None:
    """Render text in Courier on a light-gray background."""
    pdf.set_fill_color(*_LIGHT_GRAY_BG)
    pdf.set_font("Courier", size=9)
    pdf.multi_cell(0, 5, _sanitize(text), border=0, fill=True, align="L",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_fill_color(*_WHITE)
    pdf.ln(2)


def _body(pdf: Any, text: str) -> None:
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 6, _sanitize(text), border=0, align="L",
                   new_x="LMARGIN", new_y="NEXT")


# ---------------------------------------------------------------------------
# Document header (page 1 only)
# ---------------------------------------------------------------------------

def _doc_header(pdf: Any, trace_id: str, generated_at: str) -> None:
    pdf.set_font("Helvetica", style="B", size=17)
    pdf.set_text_color(*_DARK_GRAY)
    pdf.cell(0, 10, "LogPulse AI - Incident Report", border=0, align="C",
             new_x="LMARGIN", new_y="NEXT")
    # Horizontal rule
    pdf.set_fill_color(*_DARK_GRAY)
    pdf.cell(0, 1, "", border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(*_MID_GRAY)
    pdf.cell(0, 6, _sanitize(f"Generated : {generated_at}"),
             border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, _sanitize(f"Trace ID  : {trace_id}"),
             border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_BLACK)
    pdf.ln(6)


# ---------------------------------------------------------------------------
# Section 1 - Incident Summary
# ---------------------------------------------------------------------------

def _section_1(pdf: Any, state: Any) -> None:
    _section_head(pdf, "1 - Incident Summary")
    cls = getattr(state, "classification", None)
    dup = getattr(state, "duplicate_count", 1) or 1
    _kv(pdf, "Incident ID", _s(getattr(state, "trace_id", None), "N/A")[:8])
    _kv(pdf, "Category",    _s(getattr(cls, "category", None) if cls else None))
    _kv(pdf, "Severity",    _s(getattr(cls, "severity", None) if cls else None))
    _kv(pdf, "Source",      _s(getattr(cls, "source", None) if cls else None))
    _kv(pdf, "Cache",       "HIT" if dup > 1 else "MISS")
    pdf.ln(3)


# ---------------------------------------------------------------------------
# Section 2 - Raw Log
# ---------------------------------------------------------------------------

def _section_2(pdf: Any, state: Any) -> None:
    _section_head(pdf, "2 - Raw Log")
    _mono_block(pdf, _safe(getattr(state, "raw_log", None)))


# ---------------------------------------------------------------------------
# Section 3 - Classification
# ---------------------------------------------------------------------------

def _section_3(pdf: Any, state: Any) -> None:
    _section_head(pdf, "3 - Classification")
    cls = getattr(state, "classification", None)
    _kv(pdf, "Category", _s(getattr(cls, "category", None) if cls else None))
    _kv(pdf, "Severity", _s(getattr(cls, "severity", None) if cls else None))
    _kv(pdf, "Summary",  _s(getattr(cls, "summary", None) if cls else None))
    _kv(pdf, "Source",   _s(getattr(cls, "source", None) if cls else None))
    pdf.ln(3)


# ---------------------------------------------------------------------------
# Section 4 - Diagnosis
# ---------------------------------------------------------------------------

def _section_4(pdf: Any, state: Any) -> None:
    _section_head(pdf, "4 - Diagnosis")
    dr = getattr(state, "diagnostic_result", None)
    if dr is None:
        _body(pdf, "No diagnostic result available.")
        pdf.ln(3)
        return

    raw_conf = getattr(dr, "confidence", None)
    try:
        confidence_pct = int(float(raw_conf or 0) * 100)
    except (ValueError, TypeError):
        confidence_pct = 0

    _kv(pdf, "Root Cause",    _s(getattr(dr, "root_cause", None)))
    _kv(pdf, "Confidence",    f"{confidence_pct}%")
    _kv(pdf, "Reasoning",     _s(getattr(dr, "reasoning", None)))
    _kv(pdf, "Source Tier",   _s(getattr(dr, "source", None)))
    _kv(pdf, "Used History",  "Yes" if getattr(dr, "used_history", False) else "No")
    _kv(pdf, "Used Playbook", "Yes" if getattr(dr, "used_playbook", False) else "No")
    pdf.ln(3)


# ---------------------------------------------------------------------------
# Section 5 - Proposed Solution
# ---------------------------------------------------------------------------

def _section_5(pdf: Any, state: Any) -> None:
    _section_head(pdf, "5 - Proposed Solution")
    sr = getattr(state, "solution_result", None)
    if sr is None:
        _body(pdf, "No solution available.")
        pdf.ln(3)
        return

    pdf.set_font("Helvetica", style="B", size=10)
    pdf.cell(0, 6, "Explanation:", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.set_x(pdf.l_margin + 6)
    pdf.multi_cell(pdf.epw - 6, 6,
                   _sanitize(_safe(getattr(sr, "explanation", None))),
                   border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    steps = _safe_list(getattr(sr, "steps", None))
    if steps:
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 6, "Steps:", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        for i, step in enumerate(steps, 1):
            pdf.set_x(pdf.l_margin + 6)
            pdf.multi_cell(pdf.epw - 6, 6,
                           _sanitize(f"{i}. {_safe(step)}"), border=0,
                           new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    commands = _safe_list(getattr(sr, "commands", None))
    if commands:
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 6, "Commands:", border=0, new_x="LMARGIN", new_y="NEXT")
        cmd_text = "\n".join(f"  $ {_safe(c)}" for c in commands)
        _mono_block(pdf, cmd_text)

    pdf.ln(2)


# ---------------------------------------------------------------------------
# Section 6 - Risk Assessment
# ---------------------------------------------------------------------------

def _section_6(pdf: Any, state: Any) -> None:
    _section_head(pdf, "6 - Risk Assessment")
    sc = getattr(state, "security_check", None)
    if sc is None:
        _body(pdf, "No risk assessment available.")
        pdf.ln(3)
        return

    risk_raw = _safe(getattr(sc, "final_risk_level", None), "Unknown")
    risk_upper = risk_raw.upper()

    # Color-coded risk label — no new_x so cursor advances by cell width (legacy mode)
    pdf.set_font("Helvetica", style="B", size=10)
    pdf.cell(44, 6, "Risk Level :", border=0)
    if "FATAL" in risk_upper:
        pdf.set_text_color(*_RED)
    elif "SAFE" in risk_upper:
        pdf.set_text_color(*_GREEN)
    else:
        pdf.set_text_color(*_AMBER)
    pdf.set_font("Helvetica", style="B", size=10)
    val_w = pdf.w - pdf.r_margin - pdf.get_x()
    pdf.multi_cell(val_w, 6, _sanitize(risk_upper), border=0,
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_BLACK)
    pdf.ln(3)

    safe_cmds    = _safe_list(getattr(sc, "safe_commands", None))
    flagged_cmds = _safe_list(getattr(sc, "flagged_commands", None))
    matched_pats = _safe_list(getattr(sc, "matched_patterns", None))

    # Safe commands
    pdf.set_font("Helvetica", style="B", size=10)
    pdf.cell(0, 6, f"Safe Commands ({len(safe_cmds)}):", border=0,
             new_x="LMARGIN", new_y="NEXT")
    if safe_cmds:
        pdf.set_text_color(*_GREEN)
        pdf.set_font("Helvetica", size=10)
        for cmd in safe_cmds:
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(pdf.epw - 4, 6,
                           _sanitize(f"[OK] {_safe(cmd)}"), border=0,
                           new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_BLACK)
    else:
        _body(pdf, "  (none)")
    pdf.ln(2)

    # Flagged commands
    pdf.set_font("Helvetica", style="B", size=10)
    pdf.cell(0, 6, f"Flagged Commands ({len(flagged_cmds)}):", border=0,
             new_x="LMARGIN", new_y="NEXT")
    if flagged_cmds:
        pdf.set_text_color(*_AMBER)
        pdf.set_font("Helvetica", size=10)
        for i, cmd in enumerate(flagged_cmds):
            pattern = matched_pats[i] if i < len(matched_pats) else "N/A"
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(pdf.epw - 4, 6,
                           _sanitize(f"[!] {_safe(cmd)}  [{_safe(pattern)}]"),
                           border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_BLACK)
    else:
        _body(pdf, "  (none)")

    pdf.ln(3)


# ---------------------------------------------------------------------------
# Section 7 - Operator Decision
# ---------------------------------------------------------------------------

def _section_7(pdf: Any, state: Any) -> None:
    _section_head(pdf, "7 - Operator Decision")
    hr = getattr(state, "human_review", None)
    if hr is None:
        _body(pdf, "No operator review recorded.")
        pdf.ln(3)
        return

    decision = _safe(getattr(hr, "decision", None), "Unknown")
    if "reject" in decision.lower():
        _body(pdf, "Operator rejected -- no execution.")
        pdf.ln(3)
        return

    _kv(pdf, "Decision",    decision.upper())
    _kv(pdf, "Executed At", _s(getattr(hr, "executed_at", None)))
    note = _safe(getattr(hr, "operator_note", None), "")
    if note and note != "N/A":
        _kv(pdf, "Note", note)

    sandbox_results = _safe_list(getattr(hr, "sandbox_results", None))
    if sandbox_results:
        pdf.ln(2)
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 6, "Sandbox Results:", border=0, new_x="LMARGIN", new_y="NEXT")
        for res in sandbox_results:
            cmd       = _s(getattr(res, "command", None))
            exit_code = _s(getattr(res, "exit_code", None), "N/A")
            exec_ms   = _s(getattr(res, "execution_time_ms", None), "0")
            stdout    = str(getattr(res, "stdout", "") or "")
            if len(stdout) > 300:
                stdout = stdout[:300] + "... [truncated]"
            _kv(pdf, "Command", cmd)
            _kv(pdf, "Exit",    exit_code)
            _kv(pdf, "Time",    f"{exec_ms}ms")
            if stdout:
                pdf.set_font("Helvetica", style="B", size=10)
                pdf.cell(0, 6, "Output:", border=0, new_x="LMARGIN", new_y="NEXT")
                _mono_block(pdf, stdout)
            pdf.ln(2)

    pdf.ln(2)


# ---------------------------------------------------------------------------
# Section 8 - Historical Context
# ---------------------------------------------------------------------------

def _section_8(pdf: Any, state: Any) -> None:
    _section_head(pdf, "8 - Historical Context")
    dup = getattr(state, "duplicate_count", 1) or 1
    _kv(pdf, "Times Seen", str(dup))

    hc = getattr(state, "history_context", None)
    is_first = True
    if hc is not None:
        is_first = getattr(hc, "is_first_occurrence", True)

    if not is_first and hc is not None:
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 6, "Past Outcomes:", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        outcome   = _s(getattr(hc, "previous_outcome", None), "Unknown")
        last_seen = _s(getattr(hc, "last_seen", None), "Unknown")
        pdf.set_x(pdf.l_margin + 6)
        pdf.multi_cell(pdf.epw - 6, 6, f"- {outcome} on {last_seen}", border=0,
                       new_x="LMARGIN", new_y="NEXT")
    else:
        _body(pdf, "First occurrence -- no prior incidents.")

    pdf.ln(3)


# ---------------------------------------------------------------------------
# Section 9 - Incident Timeline
# ---------------------------------------------------------------------------

def _section_9(pdf: Any, state: Any) -> None:
    _section_head(pdf, "9 - Incident Timeline")
    trace_id = _safe(getattr(state, "trace_id", None), "")
    timeline = build_incident_timeline(trace_id)
    _mono_block(pdf, timeline)
