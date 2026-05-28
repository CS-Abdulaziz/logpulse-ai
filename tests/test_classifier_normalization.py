"""
tests/test_classifier_normalization.py
=======================================
Unit tests for the category normalisation layer in classifier_agent.py.

These tests are fully offline — no Colab API, no ChromaDB, no LangGraph.
They exercise normalize_category() and the CATEGORY_MAP constant directly.

Background
----------
The Qwen model is trained to emit exactly six category strings:
    'Application', 'Database', 'Memory', 'Network', 'Security', 'System'

These are also the exact strings in the ChromaDB playbook metadata.
The RAG where-filter does an exact-string match, so any drift in the
classifier's output silently breaks the filtered path. normalize_category()
maps known variants to the canonical strings.

Tests
-----
1.  test_canonical_strings_pass_through_unchanged
        Each of the six canonical strings passes through normalize_category()
        unmodified (fast path, no CATEGORY_MAP lookup needed).

2.  test_known_variant_databaseerror_maps_to_database
        'DatabaseError' (case-insensitive) → 'Database'.
        This was the specific bug reported in the code review.

3.  test_all_map_entries_produce_canonical_targets
        Every entry in CATEGORY_MAP produces one of the six canonical values.
        Catches typos in the map itself.

4.  test_known_variants_map_correctly
        Spot-checks a selection of important CATEGORY_MAP entries.

5.  test_case_insensitivity
        Normalisation is case-insensitive: 'databaseERROR', 'DATABASEERROR',
        'DatabaseError' all map to 'Database'.

6.  test_unmapped_category_passes_through_unchanged
        An unknown string not in CATEGORY_MAP is returned as-is (no crash).

7.  test_unmapped_category_prints_warning
        The warning print is actually called for an unmapped string (so the
        operator sees it in logs).

8.  test_classifier_node_emits_canonical_category
        End-to-end: mock requests.post to return {"category": "DatabaseError", ...},
        invoke classifier_node, assert state.classification.category == "Database".

9.  test_classifier_node_canonical_input_unchanged
        End-to-end: mock returns canonical {"category": "Database", ...},
        assert it still comes through as "Database" (no double-mapping).

Run from project root:
    pytest tests/test_classifier_normalization.py -v
"""

from __future__ import annotations

import os
import sys
from unittest import mock

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_ROOT      = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_WORKFLOW  = os.path.join(_ROOT, "ai_core", "workflow")
_AGENTS    = os.path.join(_WORKFLOW, "agents")

for _p in (_WORKFLOW, _AGENTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import classifier_agent                                              # noqa: E402
from classifier_agent import (                                       # noqa: E402
    normalize_category,
    CATEGORY_MAP,
    _CANONICAL_CATEGORIES,
    classifier_node,
)
from state import LogState, SeverityLevel                            # noqa: E402


# ---------------------------------------------------------------------------
# Canonical list (single source of truth for this test file)
# ---------------------------------------------------------------------------
_CANONICALS = ["Application", "Database", "Memory", "Network", "Security", "System"]


# ---------------------------------------------------------------------------
# 1. Canonical strings pass through unchanged
# ---------------------------------------------------------------------------

def test_canonical_strings_pass_through_unchanged():
    """
    All six canonical strings must be returned exactly as-is, with no
    CATEGORY_MAP lookup (the fast path in normalize_category).
    """
    for cat in _CANONICALS:
        result = normalize_category(cat)
        assert result == cat, (
            f"Canonical '{cat}' was unexpectedly transformed to '{result}'."
        )


# ---------------------------------------------------------------------------
# 2. The specific reported bug: 'DatabaseError' → 'Database'
# ---------------------------------------------------------------------------

def test_known_variant_databaseerror_maps_to_database():
    """
    'DatabaseError' is the exact string that was breaking the RAG filter.
    It must normalise to 'Database'.
    """
    assert normalize_category("DatabaseError") == "Database"


# ---------------------------------------------------------------------------
# 3. Every CATEGORY_MAP entry maps to a canonical target
# ---------------------------------------------------------------------------

def test_all_map_entries_produce_canonical_targets():
    """
    Guard against typos or drift in the map itself: every value in
    CATEGORY_MAP must be one of the six canonical strings.
    """
    for variant, target in CATEGORY_MAP.items():
        assert target in _CANONICAL_CATEGORIES, (
            f"CATEGORY_MAP['{variant}'] = '{target}' is not a canonical category. "
            f"Valid values: {sorted(_CANONICAL_CATEGORIES)}"
        )


# ---------------------------------------------------------------------------
# 4. Spot-check important map entries
# ---------------------------------------------------------------------------

def test_known_variants_map_correctly():
    """
    Spot-checks a selection of the most critical CATEGORY_MAP entries.
    """
    cases = [
        # (input,              expected)
        ("databaseerror",      "Database"),
        ("db",                 "Database"),
        ("memoryleak",         "Memory"),
        ("oom",                "Memory"),
        ("out of memory",      "Memory"),
        ("networking",         "Network"),
        ("dns",                "Network"),
        ("app",                "Application"),
        ("applicationerror",   "Application"),
        ("auth",               "Security"),
        ("authentication",     "Security"),
        ("kernel",             "System"),
        ("hardware",           "System"),
        ("os",                 "System"),
    ]
    for raw, expected in cases:
        result = normalize_category(raw)
        assert result == expected, (
            f"normalize_category('{raw}') returned '{result}', expected '{expected}'."
        )


# ---------------------------------------------------------------------------
# 5. Case-insensitivity
# ---------------------------------------------------------------------------

def test_case_insensitivity():
    """
    Normalisation must be case-insensitive for CATEGORY_MAP lookups.
    Canonical strings must still pass through exactly (case-sensitive fast path).
    """
    variants = ["databaseerror", "DatabaseError", "DATABASEERROR", "dataBaseError"]
    for v in variants:
        assert normalize_category(v) == "Database", (
            f"normalize_category('{v}') did not return 'Database'."
        )


# ---------------------------------------------------------------------------
# 6. Unmapped category passes through unchanged
# ---------------------------------------------------------------------------

def test_unmapped_category_passes_through_unchanged():
    """
    An unknown category string must be returned as-is so it is visible in
    state and logs rather than silently swallowed.
    """
    unknown = "XYZCompletelyUnknown"
    result = normalize_category(unknown)
    assert result == unknown, (
        f"Expected '{unknown}' to pass through unchanged, got '{result}'."
    )


# ---------------------------------------------------------------------------
# 7. Unmapped category triggers a warning print
# ---------------------------------------------------------------------------

def test_unmapped_category_prints_warning(capsys):
    """
    normalize_category must print a visible warning when it cannot map the
    input so that operators see the unknown value in logs.
    """
    normalize_category("SomeUnknownCategoryString")
    captured = capsys.readouterr()
    assert "WARNING" in captured.out, (
        "Expected a WARNING print for an unmapped category, got none."
    )
    assert "SomeUnknownCategoryString" in captured.out, (
        "WARNING message should include the unknown category string."
    )


# ---------------------------------------------------------------------------
# 8. classifier_node end-to-end: variant input → canonical output
# ---------------------------------------------------------------------------

def test_classifier_node_emits_canonical_category():
    """
    Mock requests.post to return {"category": "DatabaseError", ...}.
    After classifier_node runs, state.classification.category must be "Database".
    """
    mock_response = mock.MagicMock()
    mock_response.json.return_value = {
        "category": "DatabaseError",
        "source":   "Kubernetes",
        "severity": "Fatal",
        "summary":  "Connection pool exhausted.",
    }
    mock_response.raise_for_status.return_value = None

    with mock.patch("classifier_agent.requests.post", return_value=mock_response):
        state = LogState(
            raw_log="[2026-05-26 14:32:01] FATAL: remaining connection slots reserved"
        )
        result = classifier_node(state)

    assert result["classification"].category == "Database", (
        f"Expected 'Database', got '{result['classification'].category}'."
    )


# ---------------------------------------------------------------------------
# 9. classifier_node end-to-end: canonical input stays canonical
# ---------------------------------------------------------------------------

def test_classifier_node_canonical_input_unchanged():
    """
    Mock returns an already-canonical category 'Database'. It must come through
    as 'Database' — not double-transformed or altered.
    """
    mock_response = mock.MagicMock()
    mock_response.json.return_value = {
        "category": "Database",
        "source":   "Kubernetes",
        "severity": "Critical",
        "summary":  "Connection pool exhausted.",
    }
    mock_response.raise_for_status.return_value = None

    with mock.patch("classifier_agent.requests.post", return_value=mock_response):
        state = LogState(
            raw_log="[2026-05-26 14:32:01] ERROR: connection pool exhausted"
        )
        result = classifier_node(state)

    assert result["classification"].category == "Database", (
        f"Expected canonical 'Database' to pass through, got '{result['classification'].category}'."
    )
