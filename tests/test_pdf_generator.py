"""
tests/test_pdf_generator.py
============================
Unit tests for ai_core/reports/pdf_generator.py

Tests
-----
1. Report file is created at the correct path
2. File size > 0 bytes (not an empty PDF)
3. None fields produce "N/A" without crashing
4. Fields longer than 500 chars are truncated
5. fpdf2 unavailable → generate_report returns None without crashing
6. reports/ directory is auto-created if missing
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

from ai_core.events.event_bus import reset_events
from ai_core.reports.pdf_generator import _safe, generate_report


# ---------------------------------------------------------------------------
# Minimal LogState stand-in (pure dataclass — no Pydantic required)
# ---------------------------------------------------------------------------

class _FakeState:
    """Minimal stand-in for LogState with enough fields to exercise the report."""

    def __init__(self, **kwargs):
        self.trace_id        = kwargs.get("trace_id", str(uuid.uuid4()))
        self.raw_log         = kwargs.get("raw_log", "OOMKilled: pod/worker-3 memory limit exceeded 512Mi")
        self.duplicate_count = kwargs.get("duplicate_count", 1)
        self.classification  = kwargs.get("classification", None)
        self.diagnostic_result = kwargs.get("diagnostic_result", None)
        self.solution_result = kwargs.get("solution_result", None)
        self.security_check  = kwargs.get("security_check", None)
        self.human_review    = kwargs.get("human_review", None)
        self.history_context = kwargs.get("history_context", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_events():
    reset_events()
    yield
    reset_events()


@pytest.fixture()
def tmp_reports_dir(tmp_path, monkeypatch):
    """Redirect _REPORTS_DIR inside pdf_generator to a temp directory."""
    from ai_core.reports import pdf_generator as mod
    original = mod._REPORTS_DIR
    mod._REPORTS_DIR = tmp_path / "reports"
    yield mod._REPORTS_DIR
    mod._REPORTS_DIR = original


@pytest.fixture()
def basic_state():
    return _FakeState()


# ---------------------------------------------------------------------------
# Test 1 — report file is created at the correct path
# ---------------------------------------------------------------------------

def test_report_file_created_at_correct_path(tmp_reports_dir, basic_state):
    """generate_report() should create a file inside reports/ with the expected name."""
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    expected_name = f"incident_{basic_state.trace_id[:8]}_{date_str}.pdf"

    result = generate_report(basic_state)

    assert result is not None, "generate_report() returned None unexpectedly"
    result_path = Path(result)
    assert result_path.exists(), f"File not found: {result_path}"
    assert result_path.name == expected_name


# ---------------------------------------------------------------------------
# Test 2 — file size > 0 bytes (valid PDF, not empty)
# ---------------------------------------------------------------------------

def test_report_file_is_not_empty(tmp_reports_dir, basic_state):
    """The generated PDF must have content (size > 0)."""
    result = generate_report(basic_state)

    assert result is not None
    assert Path(result).stat().st_size > 0, "PDF file is empty"


# ---------------------------------------------------------------------------
# Test 3 — None fields → "N/A" without crashing
# ---------------------------------------------------------------------------

def test_none_fields_produce_na_without_crash(tmp_reports_dir):
    """All optional fields set to None must not raise any exception."""
    state = _FakeState(
        classification=None,
        diagnostic_result=None,
        solution_result=None,
        security_check=None,
        human_review=None,
        history_context=None,
    )

    result = generate_report(state)  # must not raise

    assert result is not None, "generate_report() crashed on all-None state"
    assert Path(result).exists()


def test_safe_helper_none_returns_fallback():
    assert _safe(None) == "N/A"
    assert _safe(None, fallback="Unknown") == "Unknown"
    assert _safe("hello") == "hello"


# ---------------------------------------------------------------------------
# Test 4 — long fields are truncated at 500 chars
# ---------------------------------------------------------------------------

def test_long_fields_are_truncated(tmp_reports_dir):
    """Fields exceeding 500 chars should be cut to 500 + '... [truncated]'."""
    long_text = "x" * 600

    # _safe() truncation unit test
    result = _safe(long_text)
    assert result.endswith("... [truncated]")
    assert len(result) == 500 + len("... [truncated]")

    # Integration: inject long raw_log — must not raise
    state = _FakeState(raw_log="A" * 600)
    report = generate_report(state)
    assert report is not None
    assert Path(report).exists()


# ---------------------------------------------------------------------------
# Test 5 — fpdf2 unavailable → returns None, pipeline does not crash
# ---------------------------------------------------------------------------

def test_fpdf2_unavailable_returns_none_no_crash(tmp_reports_dir, basic_state):
    """If fpdf2 is not importable, generate_report() must return None silently."""
    from ai_core.reports import pdf_generator as mod

    with mock.patch.dict("sys.modules", {"fpdf": None}):
        # Re-import won't help; patch the import inside generate_report directly
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    # Use mock.patch to make 'from fpdf import FPDF' raise ImportError
    with mock.patch("builtins.__import__", side_effect=_fpdf_import_blocker):
        result = mod.generate_report(basic_state)

    assert result is None


def _fpdf_import_blocker(name, *args, **kwargs):
    if name == "fpdf":
        raise ImportError("fpdf2 not installed (mocked)")
    return __import__(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Test 6 — reports/ directory auto-created if missing
# ---------------------------------------------------------------------------

def test_reports_dir_auto_created(tmp_path, basic_state):
    """generate_report() must create the reports/ directory if it doesn't exist."""
    from ai_core.reports import pdf_generator as mod
    original = mod._REPORTS_DIR
    new_dir = tmp_path / "nonexistent" / "reports"
    assert not new_dir.exists()

    mod._REPORTS_DIR = new_dir
    try:
        result = generate_report(basic_state)
    finally:
        mod._REPORTS_DIR = original

    assert result is not None
    assert new_dir.exists(), "reports/ directory was not created"
    assert Path(result).exists()
