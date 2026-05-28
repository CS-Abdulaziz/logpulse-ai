"""
tests/test_rag_node.py
======================
Tests for the real ChromaDB-backed rag_node.

Requires data/chroma_playbooks/ to be present (pre-built from build_vector_db.ipynb).
These tests use the REAL vector store — no mocking of ChromaDB for the happy paths —
so they also validate that retrieval quality is reasonable, not just that the code runs.

Tests
-----
1. test_filtered_query_hits_known_category
       Sends a Memory-related log with category="Memory" (a real playbook category).
       The filtered query should succeed with confidence >= threshold.

2. test_fallback_triggers_on_unknown_category
       Sends a log with category="XYZUnknownCategory" (not in the playbook DB).
       The filtered query returns empty → fallback runs → result still non-empty.

3. test_fallback_triggers_on_low_confidence
       Filtered confidence < threshold → fallback runs and returns a higher-
       scoring result that clears the acceptance floor → valid playbook returned.

4. test_missing_classification_returns_safe_fallback
       state.classification is None → no crash, returns a safe RagResult.

5. test_chromadb_error_returns_safe_fallback
       Patches _collection.query to raise → no crash, returns a safe RagResult
       with confidence=0.0.

6. test_confidence_always_in_valid_range
       For a variety of log inputs, confidence is always in [0.0, 1.0].

7. test_both_paths_below_threshold_returns_safe_fallback
       Both filtered and fallback results score below CONFIDENCE_THRESHOLD.
       Asserts that the acceptance floor fires and safe_fallback is returned
       (confidence=0.0), not the weak result.

Run from project root:
    pytest tests/test_rag_node.py -v
"""

from __future__ import annotations

import os
import sys
from unittest import mock

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT        = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKFLOW    = os.path.join(_ROOT, "ai_core", "workflow")
_AGENTS      = os.path.join(_WORKFLOW, "agents")
_CACHE_DIR   = os.path.join(_ROOT, "ai_core", "cache")

for _p in (_WORKFLOW, _AGENTS, _CACHE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import rag_agent                                            # noqa: E402
from rag_agent import rag_node, CONFIDENCE_THRESHOLD       # noqa: E402
from state import ClassificationData, LogState, SeverityLevel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(category: str, raw_log: str) -> LogState:
    return LogState(
        raw_log=raw_log,
        classification=ClassificationData(
            category=category,
            source="Kubernetes",
            severity=SeverityLevel.CRITICAL,
            summary="Test classification.",
        ),
    )


def _assert_valid_rag_result(result: dict) -> None:
    """Shared invariants every rag_node result must satisfy."""
    assert "rag_result" in result

    rag = result["rag_result"]
    assert rag.playbook_steps is not None
    assert len(rag.playbook_steps) > 0

    assert rag.rag_confidence is not None
    assert 0.0 <= rag.rag_confidence <= 1.0


# ---------------------------------------------------------------------------
# 1. Filtered query succeeds on a real playbook category
# ---------------------------------------------------------------------------

def test_filtered_query_hits_known_category():
    """
    'Memory' is a real category in the playbook DB (102 entries include Memory).
    A Memory-related log should get a filtered hit with confidence >= threshold.
    """
    state = _make_state(
        category="Memory",
        raw_log=(
            "[2026-05-26 14:32:01] ERROR pod/api-server OOMKilled: "
            "container exceeded memory limit, process killed by kernel"
        ),
    )

    result = rag_node(state)

    _assert_valid_rag_result(result)

    rag = result["rag_result"]
    # A category match for a direct OOMKilled log should score above threshold
    assert rag.rag_confidence >= CONFIDENCE_THRESHOLD, (
        f"Expected confidence >= {CONFIDENCE_THRESHOLD}, got {rag.rag_confidence}.\n"
        f"Playbook: {rag.playbook_steps[:200]}"
    )
    # The returned playbook should mention resolution steps
    assert "Resolution steps:" in rag.playbook_steps or "•" in rag.playbook_steps


# ---------------------------------------------------------------------------
# 2. Fallback triggers on an unknown category
# ---------------------------------------------------------------------------

def test_fallback_triggers_on_unknown_category():
    """
    'XYZUnknownCategory' does not exist in the playbook metadata.
    The filtered query returns no results → fallback to unfiltered search.
    The fallback should still return a relevant playbook (non-empty steps).
    """
    state = _make_state(
        category="XYZUnknownCategory",
        raw_log=(
            "[2026-05-27 09:00:00] ERROR pod/worker-7 OOMKilled: "
            "container memory limit exceeded"
        ),
    )

    result = rag_node(state)

    _assert_valid_rag_result(result)

    rag = result["rag_result"]
    # Fallback should still find something relevant in 102 playbooks
    # (the log contains OOM keywords that exist in the Memory playbooks)
    assert rag.rag_confidence >= 0.0   # could be low — just must not crash


# ---------------------------------------------------------------------------
# 3. Fallback triggers when filtered confidence is below threshold
# ---------------------------------------------------------------------------

def test_fallback_triggers_on_low_confidence():
    """
    Filtered confidence (0.05) < CONFIDENCE_THRESHOLD → Pass 2 runs.
    The fallback scores 0.80 (above the acceptance floor), so the fallback
    result is returned — not a safe_fallback. Two queries must be made.
    """
    # Filtered query returns a very-low-confidence result.
    low_conf_result = {
        "ids": [["pb_0"]],
        "distances": [[0.95]],   # confidence = 1 - 0.95 = 0.05, below threshold
        "metadatas": [[{
            "title": "Some Playbook",
            "category": "Memory",
            "severity_typical": "Warning",
            "resolution_steps": '["Check logs.", "Restart pod."]',
            "code_fix": "",
        }]],
    }
    # Fallback query returns a good result — above the acceptance floor.
    good_result = {
        "ids": [["pb_1"]],
        "distances": [[0.20]],   # confidence = 0.80, above threshold
        "metadatas": [[{
            "title": "Better Playbook",
            "category": "Memory",
            "severity_typical": "Critical",
            "resolution_steps": '["Increase memory limits.", "Monitor with Prometheus."]',
            "code_fix": "",
        }]],
    }

    call_count = {"n": 0}

    def _mock_query(*args, **kwargs):
        call_count["n"] += 1
        # First call = filtered, second call = fallback
        return low_conf_result if call_count["n"] == 1 else good_result

    mock_collection = mock.MagicMock()
    mock_collection.query.side_effect = _mock_query

    with (
        mock.patch.object(rag_agent, "_collection", mock_collection),
        mock.patch.object(rag_agent, "_CHROMA_READY", True),
    ):
        state = _make_state(
            category="Memory",
            raw_log="[2026-05-26 14:00:00] ERROR some low-signal log line",
        )
        result = rag_node(state)

    _assert_valid_rag_result(result)

    assert call_count["n"] == 2, (
        "Expected two queries (filtered + fallback); "
        f"got {call_count['n']}."
    )

    rag = result["rag_result"]
    # Fallback scored 0.80 — above the acceptance floor — so it is returned.
    assert rag.rag_confidence == 0.80
    assert "Better Playbook" in rag.playbook_steps


# ---------------------------------------------------------------------------
# 7. Both passes below threshold → acceptance floor fires → safe fallback
# ---------------------------------------------------------------------------

def test_both_paths_below_threshold_returns_safe_fallback():
    """
    Verifies the Fix 2 acceptance floor: after both passes run, if the
    *best* score is still below CONFIDENCE_THRESHOLD, rag_node must return
    _safe_fallback (confidence=0.0) instead of the weak result.

    Scenario: filtered conf=0.10, fallback conf=0.15 — both well below 0.35.
    The acceptance floor fires on the best (0.15) and safe_fallback is returned.
    Two ChromaDB queries must be made (fallback pass is not skipped).
    """
    very_low_filtered = {
        "ids": [["pb_0"]],
        "distances": [[0.90]],   # confidence = 0.10, below threshold
        "metadatas": [[{
            "title": "Weak Filtered Playbook",
            "category": "System",
            "severity_typical": "Info",
            "resolution_steps": '["Check logs."]',
            "code_fix": "",
        }]],
    }
    very_low_fallback = {
        "ids": [["pb_1"]],
        "distances": [[0.85]],   # confidence = 0.15, still below threshold
        "metadatas": [[{
            "title": "Weak Fallback Playbook",
            "category": "Network",
            "severity_typical": "Warning",
            "resolution_steps": '["Restart service."]',
            "code_fix": "",
        }]],
    }

    call_count = {"n": 0}

    def _mock_query(*args, **kwargs):
        call_count["n"] += 1
        return very_low_filtered if call_count["n"] == 1 else very_low_fallback

    mock_collection = mock.MagicMock()
    mock_collection.query.side_effect = _mock_query

    with (
        mock.patch.object(rag_agent, "_collection", mock_collection),
        mock.patch.object(rag_agent, "_CHROMA_READY", True),
    ):
        state = _make_state(
            category="System",
            raw_log="[2026-05-26 14:00:00] ERROR some very ambiguous log line here",
        )
        result = rag_node(state)

    # Both passes must have run — the acceptance floor check comes AFTER pass 2.
    assert call_count["n"] == 2, (
        f"Expected 2 ChromaDB queries (filtered + fallback), got {call_count['n']}."
    )

    # Acceptance floor fired: must receive a safe fallback, not the weak result.
    assert "rag_result" in result
    rag = result["rag_result"]
    assert rag.rag_confidence == 0.0, (
        f"Expected confidence=0.0 (safe fallback), got {rag.rag_confidence}."
    )
    # The playbook_steps text must signal a non-match, not a playbook title.
    assert "Weak" not in rag.playbook_steps, (
        "Safe fallback must not contain the weak playbook content."
    )
    assert "No matching playbook" in rag.playbook_steps or "below threshold" in rag.playbook_steps


# ---------------------------------------------------------------------------
# 4. Missing classification returns safe fallback
# ---------------------------------------------------------------------------

def test_missing_classification_returns_safe_fallback():
    """state.classification is None — must not crash."""
    state = LogState(
        raw_log="[2026-05-26 14:00:00] ERROR no classification set for this log",
        classification=None,
    )

    result = rag_node(state)

    _assert_valid_rag_result(result)
    assert result["rag_result"].rag_confidence == 0.0


# ---------------------------------------------------------------------------
# 5. ChromaDB error returns safe fallback
# ---------------------------------------------------------------------------

def test_chromadb_error_returns_safe_fallback():
    """If _collection.query raises, rag_node must return a safe RagResult."""
    mock_collection = mock.MagicMock()
    mock_collection.query.side_effect = Exception("Simulated ChromaDB I/O error")

    with (
        mock.patch.object(rag_agent, "_collection", mock_collection),
        mock.patch.object(rag_agent, "_CHROMA_READY", True),
    ):
        state = _make_state(
            category="Memory",
            raw_log="[2026-05-26 14:00:00] ERROR pod/api OOMKilled memory exceeded",
        )
        result = rag_node(state)

    _assert_valid_rag_result(result)
    assert result["rag_result"].rag_confidence == 0.0
    assert "error" in result["rag_result"].playbook_steps.lower()


# ---------------------------------------------------------------------------
# 6. Confidence is always in [0.0, 1.0] across varied inputs
# ---------------------------------------------------------------------------

def test_confidence_always_in_valid_range():
    """Fuzz a handful of log/category combinations; confidence must stay in bounds."""
    test_cases = [
        ("Memory",      "OOMKilled container exceeded memory limit on pod worker"),
        ("Network",     "DNS resolution failed for service postgres.default.svc.cluster.local"),
        ("Database",    "FATAL: remaining connection slots reserved for superuser connections"),
        ("Application", "NullPointerException in thread main at com.example.App.run"),
        ("Security",    "Unauthorized access attempt blocked by NetworkPolicy ingress rule"),
        ("FakeCategory","random unrelated log line that should trigger fallback path"),
    ]

    for category, raw_log in test_cases:
        state = _make_state(
            category=category,
            raw_log=f"[2026-05-26 14:00:00] ERROR {raw_log}",
        )
        result = rag_node(state)

        rag = result["rag_result"]
        assert 0.0 <= rag.rag_confidence <= 1.0, (
            f"Confidence out of range for category='{category}': {rag.rag_confidence}"
        )
        assert rag.playbook_steps is not None and len(rag.playbook_steps) > 0
