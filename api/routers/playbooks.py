"""api/routers/playbooks.py — Serves playbooks from ChromaDB."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from api.models.schemas import PlaybookItem

router = APIRouter(tags=["playbooks"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CHROMA_PATH  = _PROJECT_ROOT / "data" / "chroma_playbooks"

# Module-level ChromaDB collection — None if unavailable
_collection = None

try:
    import chromadb  # type: ignore

    _client     = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    _collection = _client.get_collection("playbooks")
    print(f"[Playbooks] ChromaDB ready — {_collection.count()} playbooks indexed.")
except Exception as _e:
    print(f"[Playbooks] WARNING: ChromaDB unavailable: {_e}")


# ── GET /api/playbooks ────────────────────────────────────────────────────────

@router.get("/playbooks", response_model=List[PlaybookItem])
async def list_playbooks(
    category: Optional[str] = Query(default=None, description="Filter by category"),
    search:   Optional[str] = Query(default=None, description="Semantic search query"),
    limit:    int           = Query(default=20, ge=1, le=100),
) -> list:
    if _collection is None:
        raise HTTPException(status_code=503, detail="ChromaDB not available")

    try:
        if search:
            # Semantic search
            where = {"category": category} if category else None
            kwargs: dict = {
                "query_texts": [search],
                "n_results":   min(limit, _collection.count()),
                "include":     ["metadatas"],
            }
            if where:
                kwargs["where"] = where
            results   = _collection.query(**kwargs)
            metadatas = results.get("metadatas", [[]])[0]
            ids       = results.get("ids", [[]])[0]
        else:
            # Listing
            where = {"category": category} if category else None
            kwargs = {
                "limit":   limit,
                "include": ["metadatas"],
            }
            if where:
                kwargs["where"] = where
            results   = _collection.get(**kwargs)
            metadatas = results.get("metadatas", [])
            ids       = results.get("ids", [])

        return [_meta_to_item(id_, meta) for id_, meta in zip(ids, metadatas)]

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ChromaDB error: {exc}") from exc


def _meta_to_item(id_: str, meta: dict) -> dict:
    steps_raw = meta.get("resolution_steps", "[]")
    try:
        steps = json.loads(steps_raw) if steps_raw else []
    except (json.JSONDecodeError, TypeError):
        steps = []

    return {
        "id":               id_,
        "title":            meta.get("title", "Unknown"),
        "category":         meta.get("category", "Unknown"),
        "severity_typical": meta.get("severity_typical"),
        "resolution_steps": steps,
        "code_fix":         meta.get("code_fix") or None,
        "source":           meta.get("source") or None,
    }
