"""
classifier_agent.py — Qwen LLM classification node for the LogPulse pipeline.

The Colab inference endpoint wraps a fine-tuned Qwen2.5-1.5B-Instruct model
(LoRA adapter: AbdulazizCS/logpulse-qwen-classifier).

Category vocabulary
-------------------
The model was trained (LogPulse_Qwen_clean.ipynb, Cell 19 SYSTEM_PROMPT) to
emit exactly these six category strings:

    'Application', 'Database', 'Memory', 'Network', 'Security', 'System'

These are also the exact strings stored in the ChromaDB playbook metadata,
so any category the model emits flows directly into the RAG where-filter.

However, the live inference prompt (test_model_inference notebook, Cell 3)
uses a loose schema ("category": "string") without an explicit enumeration.
Under distribution shift the model can emit variants such as "DatabaseError"
or "application" that break the exact-match filter.  CATEGORY_MAP below
normalises these known variants back to the canonical strings.

Source vocabulary (for reference — not normalised here):
    'Docker', 'Kubernetes', 'Server', 'Database', 'OS', 'Network_Device'
"""

import json
import re
import requests

from typing import Dict, Any

from ai_core.workflow.state import (
    LogState,
    ClassificationData,
    SeverityLevel
)
from ai_core.events.recorder import record
from ai_core.events.models import EventType

# ==========================================
# API Configuration
# ==========================================

COLAB_API_URL = " https://graded-regalia-unused.ngrok-free.dev/generate"

REQUEST_TIMEOUT = 45


# ==========================================
# Category Normalisation
# ==========================================

# Canonical ChromaDB category strings — must match playbooks_fixed.json exactly.
_CANONICAL_CATEGORIES = {
    "Application",
    "Database",
    "Memory",
    "Network",
    "Security",
    "System",
}

# Maps every known Qwen output variant → canonical ChromaDB category.
#
# How to extend: if a new variant is spotted in logs, add it here.
# Do NOT add the canonical strings themselves (they are handled by the
# lookup below without being listed here).
#
# Source of truth for canonical targets: data/playbooks_fixed.json
#   `python -c "import json; print(sorted(set(p['category'] for p in json.load(open('data/playbooks_fixed.json')))))`
#   → ['Application', 'Database', 'Memory', 'Network', 'Security', 'System']
CATEGORY_MAP: Dict[str, str] = {
    # ── Database variants ──────────────────────────────────────────
    "databaseerror":    "Database",   # seen in early test fixtures
    "db":               "Database",
    "database error":   "Database",
    "storage":          "Database",

    # ── Memory variants ────────────────────────────────────────────
    "memoryleak":       "Memory",
    "memory leak":      "Memory",
    "oom":              "Memory",
    "out of memory":    "Memory",

    # ── Network variants ───────────────────────────────────────────
    "networking":       "Network",
    "net":              "Network",
    "connectivity":     "Network",
    "dns":              "Network",

    # ── Application variants ───────────────────────────────────────
    "app":              "Application",
    "applicationerror": "Application",
    "application error":"Application",
    "service":          "Application",

    # ── Security variants ──────────────────────────────────────────
    "auth":             "Security",
    "authentication":   "Security",
    "authorization":    "Security",
    "securityerror":    "Security",

    # ── System variants ────────────────────────────────────────────
    "os":               "System",
    "kernel":           "System",
    "systererror":      "System",
    "system error":     "System",
    "hardware":         "System",
    "daemon":           "System",
}


def normalize_category(raw_category: str) -> str:
    """
    Normalise a raw category string emitted by Qwen to one of the six canonical
    ChromaDB values: 'Application', 'Database', 'Memory', 'Network',
    'Security', 'System'.

    Lookup order:
      1. If the string is already canonical (exact case-sensitive match), return it.
      2. Try a case-insensitive lookup in CATEGORY_MAP.
      3. If no mapping exists, log a warning and return the original string unchanged
         so the mismatch is visible in logs rather than silently discarded.

    The caller (classifier_node) is the only place this should be called — the
    .category field on ClassificationData is canonical by the time it leaves
    this node.
    """
    # Step 1: already canonical — fast path, no transformation needed
    if raw_category in _CANONICAL_CATEGORIES:
        return raw_category

    # Step 2: case-insensitive lookup in CATEGORY_MAP
    normalised = CATEGORY_MAP.get(raw_category.strip().lower())
    if normalised is not None:
        return normalised

    # Step 3: unmapped — warn and pass through so it's visible in logs
    print(
        f"[Classifier] WARNING: unknown category '{raw_category}' not in CATEGORY_MAP. "
        f"Passing through unchanged — RAG filtered path will not fire for this value. "
        f"Consider adding it to CATEGORY_MAP in classifier_agent.py."
    )
    return raw_category


# ==========================================
# Helper Functions
# ==========================================

def normalize_severity(value: str) -> SeverityLevel:

    if not value:
        return SeverityLevel.INFO

    value = value.strip().lower()

    mapping = {
        "info":     SeverityLevel.INFO,
        "warning":  SeverityLevel.WARNING,
        "critical": SeverityLevel.CRITICAL,
        "fatal":    SeverityLevel.FATAL
    }

    return mapping.get(value, SeverityLevel.INFO)


def extract_json_from_text(text: str) -> dict:
    """
    Extracts JSON safely from noisy model output.
    """

    try:

        match = re.search(r"\{.*\}", text, re.DOTALL)

        if not match:
            return {}

        return json.loads(match.group(0))

    except Exception as e:

        print(f"[Error] JSON parsing failed: {e}")

        return {}


# ==========================================
# LangGraph Node
# ==========================================

def classifier_node(state: LogState) -> Dict[str, Any]:

    print("[Agent] Custom Qwen Classifier (Colab API) analyzing log...")

    try:

        payload = {
            "prompt": state.raw_log
        }

        response = requests.post(
            COLAB_API_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        response_data = response.json()

        # ==========================================
        # Case 1: API already returns clean JSON
        # ==========================================

        if "category" in response_data:

            parsed_data = response_data

        # ==========================================
        # Case 2: API returns text inside "response"
        # ==========================================

        else:

            raw_text = response_data.get("response", "")

            parsed_data = extract_json_from_text(raw_text)

        # Normalise category before constructing ClassificationData so the
        # .category field is canonical by the time it leaves this node and
        # reaches the RAG where-filter.
        raw_category = parsed_data.get("category", "Unknown")
        canonical_category = normalize_category(raw_category)

        classification = ClassificationData(
            category=canonical_category,
            source=parsed_data.get("source", "Unknown"),
            severity=normalize_severity(
                parsed_data.get("severity")
            ),
            summary=parsed_data.get(
                "summary",
                "No summary provided."
            )
        )

        record(
            incident_id=state.trace_id,
            event_type=EventType.INCIDENT_CLASSIFIED,
            node_name="classifier_node",
            message=f"{canonical_category} / {classification.severity.value}",
            metadata={"category": canonical_category, "source": classification.source},
        )

        return {
            "classification": classification
        }

    except requests.exceptions.Timeout:

        print("⚠️ [Timeout] Colab API request timed out.")

    except requests.exceptions.ConnectionError:

        print("⚠️ [Connection Error] Could not connect to Colab API.")

    except requests.exceptions.HTTPError as e:

        print(f"⚠️ [HTTP Error] {e}")

    except Exception as e:

        print(f"⚠️ [Unexpected Error] {e}")

    # ==========================================
    # Safe Fallback
    # ==========================================

    fallback_cls = ClassificationData(
        category="Unknown",
        source="Fallback",
        severity=SeverityLevel.INFO,
        summary="Fallback classification triggered due to inference failure."
    )
    record(
        incident_id=state.trace_id,
        event_type=EventType.INCIDENT_CLASSIFIED,
        node_name="classifier_node",
        message="Fallback / Info",
        metadata={"source": "Fallback"},
    )
    return {"classification": fallback_cls}