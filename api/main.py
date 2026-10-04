"""api/main.py — LogPulse AI FastAPI application."""
from __future__ import annotations

import sys
from pathlib import Path

# ── Force UTF-8 stdout/stderr on Windows (ai_core nodes print emoji) ─────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Ensure project root is on the Python path so ai_core imports resolve ─────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from api.routers import incidents, playbooks, stats, telemetry

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LogPulse AI",
    version="0.7.0",
    description="Production SRE incident analysis — REST + SSE API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(incidents.router, prefix="/api")
app.include_router(playbooks.router, prefix="/api")
app.include_router(stats.router,     prefix="/api")
app.include_router(telemetry.router, prefix="/api")


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "version": "0.7.0"}


# ── Startup: verify ai_core + ChromaDB are reachable ─────────────────────────

@app.on_event("startup")
async def startup_checks() -> None:
    # Wipe any stale in-memory state left over from a previous uvicorn session
    # (e.g. hot-reload).  All lingering approval-gate threading.Events are set
    # first so that any still-running worker threads unblock immediately.
    from api.storage.memory import store as _store
    _store.purge_all()
    print("[Startup] In-memory store cleared — fresh session.")

    errors: list[str] = []

    # 1. ai_core imports
    try:
        from ai_core.workflow.agents.classifier_agent import classifier_node   # noqa: F401
        from ai_core.workflow.agents.rag_agent        import rag_node          # noqa: F401
        from ai_core.workflow.agents.diagnostic_agent import diagnostic_agent_node  # noqa: F401
        from ai_core.workflow.agents.solution_agent   import solution_agent_node    # noqa: F401
        from ai_core.workflow.agents.risk_assessor    import risk_assessor_node     # noqa: F401
        from ai_core.workflow.graph import (
            workflow_start_node,    # noqa: F401
            workflow_complete_node, # noqa: F401
            orchestrator_node,      # noqa: F401
            merge_context_node,     # noqa: F401
        )
        from ai_core.cache.cache_node import cache_check_node   # noqa: F401
        from ai_core.workflow.agents.history_agent import history_node  # noqa: F401
        print("[Startup] ai_core pipeline nodes loaded.")
    except Exception as exc:
        errors.append(f"ai_core import failed: {exc}")
        print(f"[Startup] ERROR — ai_core import failed: {exc}")

    # 2. ChromaDB
    try:
        from api.routers.playbooks import _collection
        if _collection is not None:
            count = _collection.count()
            print(f"[Startup] ChromaDB collection ready — {count} playbooks.")
        else:
            errors.append("ChromaDB collection is None (check data/chroma_playbooks/)")
            print("[Startup] WARNING — ChromaDB collection unavailable.")
    except Exception as exc:
        errors.append(f"ChromaDB check failed: {exc}")
        print(f"[Startup] WARNING — ChromaDB check error: {exc}")

    # 3. History DB
    history_db = _PROJECT_ROOT / "data" / "incident_history.sqlite3"
    if history_db.exists():
        print(f"[Startup] History DB found: {history_db}")
    else:
        print(f"[Startup] WARNING — History DB not found at {history_db}")

    if errors:
        print(f"[Startup] {len(errors)} warning(s). API will start but some features may be degraded.")
    else:
        print("[Startup] LogPulse API ready — pipeline nodes loaded.")
