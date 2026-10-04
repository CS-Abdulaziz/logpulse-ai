#!/usr/bin/env python3
"""
scripts/seed_history.py — Seed demo data into the LogPulse incident history DB.

Purpose
-------
Insert 2–3 realistic incidents so the demo can show recurrence behaviour
without needing to run the full workflow multiple times.

Idempotent: safe to run twice — uses the same UPSERT logic as HistoryManager,
so re-running will update the mutable fields but NOT create duplicate rows.

Usage
-----
    # From the project root:
    python scripts/seed_history.py

    # Or with an explicit DB path:
    python scripts/seed_history.py data/my_custom_history.sqlite3

Incident formats used
---------------------
These logs are drawn from the three training-distribution formats that the
LogPulse classifier handles:

  • Kubernetes (HDFS-style DFS logs): OOMKilled memory events
  • SSH / OpenSSH: failed-login brute-force attempts
  • HDFS DataNode: block replication failure
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Path bootstrap — resolve ai_core/cache/ and ai_core/workflow/agents/
# regardless of the working directory (project root, scripts/, etc.).
# ---------------------------------------------------------------------------
_SCRIPTS_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_SCRIPTS_DIR, ".."))
_CACHE_DIR    = os.path.join(_PROJECT_ROOT, "ai_core", "cache")
_WORKFLOW_DIR = os.path.join(_PROJECT_ROOT, "ai_core", "workflow")

for _p in (_CACHE_DIR, _WORKFLOW_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.history_agent import HistoryManager, SqliteHistoryBackend  # noqa: E402

# ---------------------------------------------------------------------------
# DB path (override via CLI argument)
# ---------------------------------------------------------------------------
_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "data", "incident_history.sqlite3")
_DB_PATH    = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_DB

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

# Each entry is a dict matching HistoryManager.record_incident() kwargs,
# plus an "occurrence_count" to simulate multiple runs of the same incident.
#
# We achieve a specific occurrence_count by calling record_incident() that
# many times.  The UPSERT model increments occurrence_count on each call, so
# calling N times → occurrence_count == N.

SEED_INCIDENTS = [
    {
        # ── Incident 1: Kubernetes OOMKilled ────────────────────────────────
        # Simulates a container hitting its memory limit 4 times.
        # outcome: "simulated_success" — memory limit was raised.
        "raw_log": (
            "[2026-05-20 03:12:45] ERROR pod/api-gateway-7d8f9b5c6-xk2lp "
            "OOMKilled: container api-gateway exceeded memory limit 512Mi; "
            "terminated by kernel out-of-memory killer"
        ),
        "category":         "Memory",
        "source":           "Kubernetes",
        "severity":         "Critical",
        "summary":          "Container api-gateway OOMKilled — exceeded 512Mi memory limit.",
        "root_cause":       (
            "Unbound in-memory cache growth in the api-gateway service caused "
            "heap usage to spike past the configured container memory limit "
            "(512Mi), triggering the kernel OOM killer."
        ),
        "applied_solution": (
            "kubectl patch deployment api-gateway -p "
            "'{\"spec\":{\"template\":{\"spec\":{\"containers\":"
            "[{\"name\":\"api-gateway\",\"resources\":"
            "{\"limits\":{\"memory\":\"1Gi\"}}}]}}}}'"
        ),
        "outcome":          "simulated_success",
        "occurrence_count": 4,
    },
    {
        # ── Incident 2: SSH brute-force failed logins ──────────────────────
        # Simulates 2 recurrences of the same brute-force pattern.
        # outcome: "approved" — admin acknowledged and applied IP block.
        "raw_log": (
            "May 26 14:32:01 prod-bastion sshd[23841]: "
            "Failed password for invalid user admin from 203.0.113.42 port 51234 ssh2"
        ),
        "category":         "Security",
        "source":           "OpenSSH",
        "severity":         "Warning",
        "summary":          "Repeated SSH failed-login attempts for user 'admin' from external IP.",
        "root_cause":       (
            "Automated credential-stuffing tool targeting the bastion host's "
            "SSH port (22). The source IP is not in any allow-list and has no "
            "legitimate access rights."
        ),
        "applied_solution": (
            "ufw insert 1 deny from 203.0.113.0/24 to any port 22; "
            "systemctl reload ufw"
        ),
        "outcome":          "approved",
        "occurrence_count": 2,
    },
    {
        # ── Incident 3: HDFS DataNode block replication failure ────────────
        # Single prior occurrence — outcome still pending human review.
        "raw_log": (
            "081109 213254 412 INFO dfs.DataNode$BlockSender: "
            "Exception in sendChunks blk_-3544583377289625738 "
            "java.io.IOException: Broken pipe"
        ),
        "category":         "System",
        "source":           "HDFS",
        "severity":         "Warning",
        "summary":          "HDFS DataNode block send failed with Broken pipe — replication interrupted.",
        "root_cause":       (
            "Network instability between DataNode and NameNode caused the "
            "TCP connection to drop mid-transfer, resulting in a "
            "java.io.IOException: Broken pipe during block chunk send."
        ),
        "applied_solution": (
            "hdfs dfsadmin -refreshNodes; "
            "hdfs fsck / -files -blocks -locations 2>&1 | grep -i 'CORRUPT'"
        ),
        "outcome":          "simulated_success",
        "occurrence_count": 1,
    },
]

# ---------------------------------------------------------------------------
# Seeding logic
# ---------------------------------------------------------------------------

def _clear_table(db_path: str) -> None:
    """Truncate the incident_history table so seed data is always clean."""
    import sqlite3
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.execute("DELETE FROM incident_history")
        conn.commit()
        print("  (cleared existing rows for clean re-seed)\n")
    except sqlite3.OperationalError:
        pass  # Table may not exist yet on the very first run
    finally:
        conn.close()


def seed(db_path: str) -> None:
    """Insert seed incidents into *db_path* (idempotent: truncates first)."""
    # Ensure the table exists before trying to clear it
    SqliteHistoryBackend(db_path)   # constructor creates the table if absent
    _clear_table(db_path)

    backend = SqliteHistoryBackend(db_path)
    manager = HistoryManager(backend)

    print(f"Seeding history DB: {db_path}\n")

    for idx, incident in enumerate(SEED_INCIDENTS, start=1):
        target_count = incident["occurrence_count"]
        raw_log      = incident["raw_log"]

        # Build kwargs for record_incident (exclude the non-kwarg key)
        record_kwargs = {k: v for k, v in incident.items()
                        if k != "occurrence_count"}

        print(f"  [{idx}] {incident['category']} / {incident['source']}")
        print(f"       occurrence_count: {target_count}")
        print(f"       outcome: {incident['outcome']}")

        for _ in range(target_count):
            manager.record_incident(**record_kwargs)

        # Verify
        ctx = manager.get_context(raw_log)
        assert ctx.occurrence_count == target_count, (
            f"Mismatch: expected {target_count}, got {ctx.occurrence_count}"
        )
        print()

    backend.close()
    print(f"[OK] Seeding complete.  DB: {db_path}")


if __name__ == "__main__":
    seed(_DB_PATH)
