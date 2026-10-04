"""
rag_agent.py — ChromaDB-backed RAG retrieval for the LogPulse pipeline.

Strategy: Hybrid category filter
---------------------------------
The Qwen classifier achieves ~90.8% category accuracy, meaning ~9% of
classifications land in the wrong category. A hard filter would silently
return no results in those cases. We use a two-pass approach:

  Pass 1 (precision): query with a ``where={"category": ...}`` filter,
      restricting to playbooks in the classified category. Fast and
      precise when the classifier is right.

  Pass 2 (resilience): if Pass 1 returns no results OR the top-result
      confidence is below CONFIDENCE_THRESHOLD, run the same query
      against ALL 102 playbooks without any filter. This rescues the
      ~9% misclassification cases.

Threshold semantics (Fix 2)
----------------------------
CONFIDENCE_THRESHOLD is now a genuine acceptance floor, not merely a
fallback trigger. After both passes, the *best* of the two scores is
selected. If that best score is still below the threshold, a safe
fallback is returned instead of a weak result — so downstream nodes can
treat rag_confidence=0.0 as "RAG had no confident match" rather than
receiving a plausible-looking but unreliable playbook.

Category vocabulary (exact strings stored in ChromaDB metadata)
---------------------------------------------------------------
'Application', 'Database', 'Memory', 'Network', 'Security', 'System'

IMPORTANT: the where-filter is an exact-string match. The Qwen classifier
must emit one of these six strings for the filtered path (Pass 1) to fire.
If it emits e.g. "DatabaseError" instead of "Database", the filter returns
zero results and the query silently falls through to Pass 2 every time.
Verify classifier output against this vocabulary during integration.

ChromaDB config (matches build_vector_db.ipynb):
  - Embedding model : all-MiniLM-L6-v2 via ONNX  (ChromaDB default)
  - Distance metric : cosine  (hnsw:space = cosine)
  - Collection name : "playbooks"
  - Indexed text    : Title + Category + Source + Symptoms + Root cause
  - Metadata keys   : title, category, source, severity_typical,
                       resolution_steps (json.dumps list), code_fix,
                       internal_note
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from ai_core.workflow.state import LogState, RagResult
from ai_core.events.recorder import record
from ai_core.events.models import EventType

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

# CONFIDENCE_THRESHOLD is an acceptance floor: any result (filtered OR
# fallback) that scores below this is considered "not trustworthy" and the
# node returns a safe_fallback instead of a weak match.
CONFIDENCE_THRESHOLD: float = 0.35

# Number of candidates to retrieve per query (only the top-1 is used).
_N_RESULTS: int = 3

# When True, print a short block after every successful retrieval showing
# the matched playbook title, category, confidence, and the first few
# resolution steps. Flip to False before demos to reduce console noise.
DEBUG_RAG: bool = True

# ---------------------------------------------------------------------------
# ChromaDB client — loaded ONCE at module import
# ---------------------------------------------------------------------------

_AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))
_CHROMA_PATH = os.path.normpath(
    os.path.join(_AGENTS_DIR, "..", "..", "..", "data", "chroma_playbooks")
)

_collection = None   # typed: chromadb.Collection | None
_CHROMA_READY = False

try:
    import chromadb                                                   # type: ignore
    _client     = chromadb.PersistentClient(path=_CHROMA_PATH)
    _collection = _client.get_collection("playbooks")
    _CHROMA_READY = True
    print(f"[RAG] ChromaDB ready — {_collection.count()} playbooks indexed.")
except Exception as _e:
    print(f"[RAG] WARNING: ChromaDB failed to initialise: {_e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _distance_to_confidence(distance: float) -> float:
    """
    Convert a ChromaDB cosine distance → similarity score in [0.0, 1.0].

    ChromaDB cosine distance = 1 − cosine_similarity for normalised vectors,
    so similarity = 1 − distance.  We clamp to [0, 1] defensively.
    """
    return round(max(0.0, min(1.0, 1.0 - distance)), 4)


def _parse_query_result(
    results: Dict[str, Any],
) -> Optional[Tuple[str, float, Dict[str, Any]]]:
    """
    Extract (playbook_steps_text, confidence, metadata) from a raw ChromaDB
    result dict.

    Returns ``None`` if the result set is empty.

    The metadata dict is returned as-is so callers can access ``title``,
    ``category``, ``resolution_steps``, etc. for debug output without
    re-parsing.
    """
    ids = results.get("ids", [[]])
    if not ids or not ids[0]:
        return None

    distance   = results["distances"][0][0]
    confidence = _distance_to_confidence(distance)
    metadata   = results["metadatas"][0][0]

    # resolution_steps is stored as json.dumps(list) — must be decoded
    steps_raw: str   = metadata.get("resolution_steps", "[]")
    steps: List[str] = json.loads(steps_raw) if steps_raw else []

    title    = metadata.get("title", "Unknown")
    severity = metadata.get("severity_typical", "")
    code_fix = metadata.get("code_fix", "").strip()

    # Build a human-readable block for downstream nodes
    lines = [
        f"[Playbook: {title}]",
        f"Severity: {severity}",
        "",
        "Resolution steps:",
    ]
    for step in steps:
        lines.append(f"  • {step}")

    if code_fix:
        lines.append("")
        lines.append("Code fix:")
        lines.append(code_fix)

    return "\n".join(lines), confidence, metadata


def _safe_fallback(reason: str) -> Dict[str, Any]:
    """Return a low-confidence RagResult when retrieval cannot produce a match."""
    return {
        "rag_result": RagResult(
            playbook_steps=f"[RAG] No matching playbook found. Reason: {reason}",
            rag_confidence=0.0,
        )
    }


def _debug_print(path: str, metadata: Dict[str, Any], confidence: float) -> None:
    """
    Print a concise retrieval quality block when DEBUG_RAG is True.

    Shows: path taken, matched playbook title + category, confidence,
    and the first 3 resolution steps.
    """
    title    = metadata.get("title", "Unknown")
    category = metadata.get("category", "Unknown")
    steps_raw: str   = metadata.get("resolution_steps", "[]")
    steps: List[str] = json.loads(steps_raw) if steps_raw else []
    preview  = steps[:3]

    print(f"[RAG DEBUG] path={path}  confidence={confidence}")
    print(f"[RAG DEBUG] matched → title='{title}'  category='{category}'")
    if preview:
        print("[RAG DEBUG] first steps:")
        for s in preview:
            print(f"[RAG DEBUG]   • {s}")


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def rag_node(state: LogState) -> Dict[str, Any]:
    """
    Retrieve the most relevant playbook for the current log from ChromaDB.

    Two-pass hybrid retrieval
    -------------------------
    Pass 1 — category-filtered query:
        Restricts the search to playbooks whose ``category`` metadata field
        matches the classifier's output.  High precision, may miss if the
        classifier was wrong.

    Pass 2 — unfiltered fallback:
        Triggered when Pass 1 returns no results or confidence < threshold.
        Searches all 102 playbooks.  Trades precision for resilience.

    Acceptance floor
    ----------------
    After both passes, the higher-scoring result is selected. If that best
    score is still below CONFIDENCE_THRESHOLD, _safe_fallback is returned so
    downstream nodes receive an explicit "not confident" signal rather than
    a weak playbook match.
    """
    # Guard: ChromaDB not available
    if not _CHROMA_READY or _collection is None:
        print("[RAG] SKIP — ChromaDB not ready.")
        return _safe_fallback("ChromaDB not initialised.")

    # Guard: classifier may have been skipped / failed
    if state.classification is None:
        print("[RAG] SKIP — no classification on state.")
        return _safe_fallback("Upstream classification missing.")

    category   = state.classification.category
    query_text = f"{category} {state.raw_log}"

    try:
        # ── Pass 1: category-filtered query ──────────────────────────────
        # Precision path — fast when the classifier is right (~90.8% of cases).
        # NOTE: the where-filter is an exact-string match against the ChromaDB
        # metadata field. Valid values: 'Application', 'Database', 'Memory',
        # 'Network', 'Security', 'System'. Any other string (e.g. "DatabaseError")
        # will return zero results and silently route to Pass 2.
        filtered_results = _collection.query(
            query_texts=[query_text],
            n_results=_N_RESULTS,
            where={"category": category},
        )
        filtered_parsed = _parse_query_result(filtered_results)

        if filtered_parsed is not None:
            filtered_steps, filtered_conf, filtered_meta = filtered_parsed
        else:
            filtered_steps, filtered_conf, filtered_meta = None, 0.0, {}

        use_filtered = (
            filtered_parsed is not None
            and filtered_conf >= CONFIDENCE_THRESHOLD
        )

        if use_filtered:
            # Filtered result is above the acceptance floor — return it directly.
            print(
                f"[RAG] HIT (filtered) — category='{category}', "
                f"confidence={filtered_conf}"
            )
            if DEBUG_RAG:
                _debug_print("filtered", filtered_meta, filtered_conf)
            record(
                incident_id=state.trace_id,
                event_type=EventType.RAG_RETRIEVED,
                node_name="rag_node",
                message=f"Playbook retrieved (filtered) — {category} conf={filtered_conf:.2f}",
                metadata={"path": "filtered", "confidence": filtered_conf, "category": category},
            )
            return {
                "rag_result": RagResult(
                    playbook_steps=filtered_steps,
                    rag_confidence=filtered_conf,
                )
            }

        # ── Pass 2: unfiltered fallback ───────────────────────────────────
        # Resilience path — handles misclassifications and unknown categories.
        reason = (
            "filtered result empty"
            if filtered_parsed is None
            else f"filtered confidence {filtered_conf} < threshold {CONFIDENCE_THRESHOLD}"
        )
        print(f"[RAG] Falling back to unfiltered search ({reason}).")

        fallback_results = _collection.query(
            query_texts=[query_text],
            n_results=_N_RESULTS,
        )
        fallback_parsed = _parse_query_result(fallback_results)

        if fallback_parsed is None:
            print("[RAG] MISS — no results from unfiltered search either.")
            return _safe_fallback("No results from unfiltered search.")

        fallback_steps, fallback_conf, fallback_meta = fallback_parsed

        # ── Pick the better of the two results ───────────────────────────
        if filtered_parsed is not None and filtered_conf >= fallback_conf:
            best_steps, best_conf, best_meta, best_path = (
                filtered_steps, filtered_conf, filtered_meta, "filtered"
            )
            print(f"[RAG] Using filtered result over fallback (conf={filtered_conf}).")
        else:
            best_steps, best_conf, best_meta, best_path = (
                fallback_steps, fallback_conf, fallback_meta, "fallback"
            )
            print(f"[RAG] HIT (fallback) — confidence={fallback_conf}")

        # ── Acceptance floor — reject even the best result if too weak ────
        # Both passes produced results, but neither scored above the threshold.
        # Returning a weak result as if it were trustworthy would mislead the
        # diagnostic agent. Surface this as an explicit low-confidence signal.
        if best_conf < CONFIDENCE_THRESHOLD:
            print(
                f"[RAG] REJECT — best score {best_conf} still below "
                f"threshold {CONFIDENCE_THRESHOLD}. Returning safe fallback."
            )
            return _safe_fallback(
                f"best confidence {best_conf} below threshold {CONFIDENCE_THRESHOLD}"
            )

        if DEBUG_RAG:
            _debug_print(best_path, best_meta, best_conf)

        record(
            incident_id=state.trace_id,
            event_type=EventType.RAG_RETRIEVED,
            node_name="rag_node",
            message=f"Playbook retrieved ({best_path}) conf={best_conf:.2f}",
            metadata={"path": best_path, "confidence": best_conf},
        )
        return {
            "rag_result": RagResult(
                playbook_steps=best_steps,
                rag_confidence=best_conf,
            )
        }

    except Exception as exc:
        print(f"[RAG] ERROR during retrieval: {exc}")
        return _safe_fallback(f"retrieval error: {exc}")
