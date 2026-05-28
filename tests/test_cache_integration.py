"""
tests/test_cache_integration.py
================================
End-to-end integration test: proves that a cache HIT causes the full
LangGraph pipeline to bypass classifier_node entirely, making zero calls
to the Colab LLM API.

Why this is distinct from the 11 unit tests
--------------------------------------------
The unit tests validate hashing, backend CRUD, and node guards in
isolation. THIS test drives the *compiled LangGraph graph* (logpulse_app)
end-to-end and checks that the conditional edge genuinely routes around
classifier_node on the second run — not just that the cache returns data,
but that the graph never reaches the LLM node at all.

Tests
-----
1. test_classifier_called_exactly_once_across_two_runs
       Core guarantee: two runs of the same incident → LLM called once.
2. test_route_after_cache_hit
       route_after_cache returns "cache_hit" when classification is set.
3. test_route_after_cache_miss
       route_after_cache returns "cache_miss" when classification is None.
4. test_brand_new_log_is_a_miss_after_cache_populated
       A third, semantically different log is a MISS even when the cache
       already holds an entry — confirms no false-positive collisions.

Run from project root:
    pytest tests/test_cache_integration.py -v
    pytest tests/ -v          # run together with the 11 unit tests
"""

from __future__ import annotations

import os
import sys
from unittest import mock

# ---------------------------------------------------------------------------
# Path bootstrap — must happen before any project imports so both
# `ai_core/cache/` and `ai_core/workflow/` are resolvable from the project
# root (the natural pytest working directory).
#
# Safety note: cache_node.py uses InMemoryCacheBackend at module level,
# so importing it never touches the filesystem / data/ directory.
# ---------------------------------------------------------------------------
_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_CACHE_DIR = os.path.join(_ROOT, "ai_core", "cache")
_WORKFLOW_DIR = os.path.join(_ROOT, "ai_core", "workflow")

for _p in (_CACHE_DIR, _WORKFLOW_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from graph import logpulse_app                              # noqa: E402
from cache_node import configure_cache, route_after_cache   # noqa: E402
from cache_manager import InMemoryCacheBackend              # noqa: E402
from state import ClassificationData, LogState, SeverityLevel  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Two real-looking log lines — same incident, different dynamic fields only
_LOG_RUN1 = (
    "[2026-05-26 14:32:01] ERROR [postgres-db-01] "
    "pid=1234 192.168.1.10:5432 "
    "FATAL: remaining connection slots are reserved for non-replication superuser"
)
_LOG_RUN2 = (
    "[2026-05-27 09:15:44] ERROR [postgres-db-01] "
    "pid=9999 10.0.0.55:5432 "
    "FATAL: remaining connection slots are reserved for non-replication superuser"
)

# A completely different incident — must always be a cache MISS
_LOG_DIFFERENT = (
    "[2026-05-27 11:00:00] WARN [nginx-proxy-01] "
    "upstream timed out (110: connection timed out) while reading response header"
)

# What the mocked Colab API returns for the PostgreSQL incident.
# 'DatabaseError' is intentionally a drifted/variant string (not the canonical
# form) so this test also exercises the normalize_category() path.
# After normalisation the classification.category must be 'Database'.
_MOCK_API_PAYLOAD = {
    "category": "DatabaseError",   # variant — normalize_category maps → 'Database'
    "source": "PostgreSQL",
    "severity": "Fatal",
    "summary": "Connection pool exhausted on postgres-db-01",
}


def _make_mock_response(payload: dict) -> mock.Mock:
    """Build a mock that satisfies requests.post() → response.raise_for_status() + .json()."""
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()   # must not raise
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCacheSkipsClassifier:
    """
    Integration tests that drive the real compiled LangGraph graph.
    Each test method resets the cache to a fresh InMemoryCacheBackend so
    tests are fully isolated from each other and from the unit-test suite.
    """

    def setup_method(self):
        """Reset to an empty in-memory cache before every test and mock Gemini to avoid network calls."""
        configure_cache(InMemoryCacheBackend())
        self.diag_patcher = mock.patch("agents.diagnostic_agent.GEMINI_AVAILABLE", new=False)
        self.sol_patcher = mock.patch("agents.solution_agent.GEMINI_AVAILABLE", new=False)
        self.diag_patcher.start()
        self.sol_patcher.start()
        os.environ["LOGPULSE_AUTO_APPROVE"] = "true"
        os.environ["GEMINI_AVAILABLE"] = "false"

    def teardown_method(self):
        """Clean up the Gemini mocks."""
        self.diag_patcher.stop()
        self.sol_patcher.stop()
        os.environ.pop("LOGPULSE_AUTO_APPROVE", None)
        os.environ.pop("GEMINI_AVAILABLE", None)

    # -----------------------------------------------------------------------
    # 1. Core assertion
    # -----------------------------------------------------------------------

    def test_classifier_called_exactly_once_across_two_runs(self):
        """
        Submit the same incident twice through logpulse_app.

        Expected behaviour
        ------------------
        Run 1: cache MISS → cache_check_node returns {} →
                conditional edge routes to classifier_node →
                classifier calls requests.post (count=1) →
                cache_write_node stores the classification.

        Run 2: cache HIT → cache_check_node returns populated classification →
                conditional edge routes DIRECTLY to orchestrator_node →
                classifier_node is NEVER reached →
                requests.post is NOT called again (count still 1).

        Key assertions
        --------------
        * mock_post.call_count == 1 after BOTH runs.
        * Both results carry the same classification.
        * Run 2 duplicate_count == 2 (cache hit bumped the counter).
        """
        with mock.patch(
            "requests.post",
            return_value=_make_mock_response(_MOCK_API_PAYLOAD),
        ) as mock_post:

            # ── Run 1 ── cache MISS ──────────────────────────────────────────
            result1 = logpulse_app.invoke({"raw_log": _LOG_RUN1})

            assert mock_post.call_count == 1, (
                "Run 1 must call the classifier exactly once (cache was empty)."
            )
            # normalize_category() maps 'DatabaseError' → 'Database' (canonical)
            assert result1["classification"].category == "Database"
            assert result1["classification"].source == "PostgreSQL"
            # On a cache MISS no node writes duplicate_count, so LangGraph
            # omits it from the result dict; fall back to the LogState default.
            assert result1.get("duplicate_count", 1) == 1

            # ── Run 2 ── same incident, different timestamp / pid / ip ───────
            result2 = logpulse_app.invoke({"raw_log": _LOG_RUN2})

            # THE KEY ASSERTION: classifier must NOT have been called again
            assert mock_post.call_count == 1, (
                "Run 2 must NOT call the classifier — "
                f"requests.post was called {mock_post.call_count} times, expected 1."
            )

            # Classification is populated and correct (served from cache).
            # Cache stores and restores the already-normalised canonical string.
            assert result2["classification"].category == "Database"
            assert result2["classification"].source == "PostgreSQL"

            # duplicate_count reflects the cache hit counter
            assert result2["duplicate_count"] == 2, (
                f"Expected duplicate_count=2 on cache hit, got {result2['duplicate_count']}."
            )

    # -----------------------------------------------------------------------
    # 2 & 3. route_after_cache edge function (unit-level, no graph overhead)
    # -----------------------------------------------------------------------

    def test_route_after_cache_hit(self):
        """route_after_cache must return 'cache_hit' when classification is set."""
        state = LogState(
            raw_log="[2026-05-26 14:32:01] ERROR some meaningful error context here",
            classification=ClassificationData(
                category="NetworkError",
                source="Kubernetes",
                severity=SeverityLevel.CRITICAL,
                summary="Pod network connectivity failure detected in cluster.",
            ),
        )
        assert route_after_cache(state) == "cache_hit"

    def test_route_after_cache_miss(self):
        """route_after_cache must return 'cache_miss' when classification is None."""
        state = LogState(
            raw_log="[2026-05-26 14:32:01] ERROR some meaningful error context here",
            classification=None,
        )
        assert route_after_cache(state) == "cache_miss"

    # -----------------------------------------------------------------------
    # 4. No false-positive collisions after cache is warm
    # -----------------------------------------------------------------------

    def test_brand_new_log_is_miss_after_cache_populated(self):
        """
        After the cache holds an entry for incident A, a semantically different
        incident B must still produce a cache MISS and call the classifier.

        This validates that normalisation does not over-aggressively collapse
        unrelated log patterns into the same hash.
        """
        with mock.patch(
            "requests.post",
            return_value=_make_mock_response(_MOCK_API_PAYLOAD),
        ) as mock_post:

            # Warm the cache with incident A
            logpulse_app.invoke({"raw_log": _LOG_RUN1})
            assert mock_post.call_count == 1  # sanity

            # Submit a completely different incident
            logpulse_app.invoke({"raw_log": _LOG_DIFFERENT})

            assert mock_post.call_count == 2, (
                "A new, unrelated log must produce a cache MISS and call the classifier. "
                f"requests.post was called {mock_post.call_count} times, expected 2."
            )
