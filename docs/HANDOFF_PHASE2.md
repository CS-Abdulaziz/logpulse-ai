# LogPulse AI — Phase 2 Checkpoint Handoff

> **Status:** Phase 2 Complete · Phase 3 Pending
> **Date:** 2026-05-27
> **Author:** CS-Abdulaziz
> **Document Purpose:** Living technical handoff for the LogPulse AI-driven SRE automation pipeline. Evolves the Phase 1 foundation to include the newly introduced Intelligent Cache Layer. This document is the single authoritative reference for Phase 3 development.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What's New in Phase 2](#2-whats-new-in-phase-2)
3. [Updated Hybrid Architecture Deep Dive](#3-updated-hybrid-architecture-deep-dive)
4. [New Module Breakdown — Phase 2 Delta](#4-new-module-breakdown--phase-2-delta)
5. [Updated End-to-End Data Flow](#5-updated-end-to-end-data-flow)
6. [Updated File Map](#6-updated-file-map)
7. [Next Steps & Phase 3 Roadmap](#7-next-steps--phase-3-roadmap)

---

## 1. Executive Summary

**LogPulse** is an AI-driven Site Reliability Engineering (SRE) automation pipeline. Its purpose is to ingest raw infrastructure logs, classify severity and category using a fine-tuned language model, and — when a fault is detected — automatically generate a validated, safe remediation plan without requiring manual triage.

### The Problem It Solves

In modern distributed systems, engineering teams are overwhelmed by log volume. A single Kubernetes cluster can produce tens of thousands of log lines per minute. Manually identifying FATAL events, correlating them with historical incidents, and producing a safe remediation script is slow, expensive, and error-prone — especially during on-call incidents at 3 AM.

### The Value Proposition

| Traditional SRE | LogPulse SRE |
|---|---|
| Engineer pages on-call → reads logs manually | Raw log ingested automatically → AI classifies in seconds |
| Searches runbooks in Confluence | RAG node retrieves relevant playbook steps from structured knowledge base |
| Engineer writes `kubectl` / `systemctl` commands from memory | Solution agent generates parameterized commands from diagnostic context |
| No pre-flight safety check on commands | Risk assessor blocks destructive patterns before any execution |
| Resolution time: 15–60 minutes | Target resolution time: < 5 minutes with human approval gate |

The system does not fully automate execution — a **Human-in-the-Loop** approval gate is deliberately built into the pipeline, ensuring an engineer reviews the AI's proposed remediation before anything touches production.

---

## 2. What's New in Phase 2

### 2.1 The Intelligent Cache Layer

Phase 2 introduces an **Intelligent Cache Layer** that intercepts duplicate or structurally identical log events *before* they reach the Colab LLM inference endpoint. This is the most significant architectural change in Phase 2.

**Core insight:** In SRE production environments, the same underlying fault (e.g., a PostgreSQL connection pool exhaustion) fires hundreds of log lines within seconds. Every one of those lines differs only in dynamic metadata — timestamps, PIDs, IP addresses, request IDs — but represents the *same incident pattern*. Without a cache, each log line triggers a full HTTP round-trip to Colab, consuming T4 GPU inference time and Colab session quota.

**What the cache does:**

1. **Normalises** each incoming log line by stripping all dynamic noise fields (timestamps, IPs, UUIDs, PIDs, hex addresses) and replacing them with stable placeholder tokens.
2. **Hashes** the normalised text with SHA-256 to produce a deterministic 64-character cache key.
3. **Looks up** the key in the active backend (in-memory dict or SQLite WAL).
4. On **hit**: reconstructs `ClassificationData` from the stored record, populates `state.classification` and `state.duplicate_count`, and routes the graph *directly* to `orchestrator_node`, bypassing the LLM entirely.
5. On **miss**: passes control to `classifier_node` as before, then writes the fresh classification to the cache for all future duplicates.

**Value proposition of the cache:**

| Metric | Without Cache | With Cache |
|---|---|---|
| LLM calls per incident storm (100 identical logs) | 100 | 1 |
| Colab API round-trip latency (per log) | ~2–5 s | ~0 ms (dict/SQLite lookup) |
| Colab session quota consumed | High | Minimal |
| Duplicate signal to downstream nodes | Unbounded | Tracked via `duplicate_count` |

### 2.2 New Files Introduced

| File | Role |
|---|---|
| `ai_core/cache/hashing.py` | Stateless log normalisation + SHA-256 hashing |
| `ai_core/cache/cache_manager.py` | Backend-agnostic storage layer (ABC + InMemory + SQLite) |
| `ai_core/cache/cache_node.py` | LangGraph node functions + conditional edge router |
| `ai_core/cache/__init__.py` | Public surface of the `cache` package |

### 2.3 Updated Files

| File | Change |
|---|---|
| `ai_core/workflow/graph.py` | Wired `cache_check_node`, `cache_write_node`, and `route_after_cache` into the DAG; added `sys.path` bootstrap for `ai_core/cache/` |

---

## 3. Updated Hybrid Architecture Deep Dive

### 3.1 Local-Cloud Split (Phase 1 Recap)

The hybrid architecture separates **orchestration** (local machine, no GPU required) from **LLM inference** (Google Colab T4 GPU, accessed via ngrok HTTPS tunnel):

```
┌──────────────────────────────────────────────────────────┐
│                   LOCAL MACHINE                          │
│                                                          │
│   LangGraph DAG Orchestrator                             │
│   ├── Intelligent Cache Layer  ← NEW in Phase 2          │
│   ├── State management (Pydantic / LogState)             │
│   ├── Routing logic (orchestrator_node)                  │
│   ├── RAG retrieval (playbooks_fixed.json)               │
│   ├── Human approval gate                                │
│   ├── Diagnostic & Solution agents                       │
│   └── Risk assessor                                      │
│                                                          │
│   All nodes are lightweight Python — no GPU required.    │
│                                                          │
└──────────────────┬───────────────────────────────────────┘
                   │  HTTP POST /generate
                   │  {only on cache miss}        ← NEW guard
                   ▼
┌──────────────────────────────────────────────────────────┐
│              GOOGLE COLAB (T4 GPU)                       │
│                                                          │
│   FastAPI Server (port 8000)                             │
│   └── Qwen2.5-1.5B + LoRA adapter (4-bit)               │
│       └── Returns {"category", "source",                 │
│                     "severity", "summary"}               │
│                                                          │
│   Exposed via: ngrok HTTPS tunnel                        │
└──────────────────────────────────────────────────────────┘
```

> **Key change from Phase 1:** The `HTTP POST /generate` call to Colab is now *conditional*. It only fires on a cache miss. All subsequent occurrences of the same log pattern are resolved locally.

### 3.2 The Exact-Match Hashing Strategy

The cache uses **content-addressed storage**: the cache key is derived entirely from the *semantic content* of the log, not its raw string.

**Normalisation pipeline (applied in strict order):**

| Rule | Pattern Matched | Token |
|---|---|---|
| 1 | ISO 8601 / RFC 3339 timestamps | `<TIMESTAMP>` |
| 2 | Syslog / cron timestamps (`May 26 14:32:01`) | `<TIMESTAMP>` |
| 3 | Bare wall-clock times (`14:32:01`) | `<TIMESTAMP>` |
| 4 | UUIDs (any variant) | `<UUID>` |
| 5 | IPv4 addresses with optional port | `<IP>` |
| 6 | Named dynamic fields: `pid=`, `tid=`, `req_id=`, `trace_id=`, `span_id=`, etc. | `<ID>` |
| 7 | `0x`-prefixed hex literals (≥4 hex digits) | `<HEX>` |
| 8 | Bare hex strings (≥8 consecutive hex chars) | `<HEX>` |
| 9 | Long integers (≥5 digits) | `<NUM>` |
| 10 | Whitespace runs (newlines, tabs, multiple spaces) | single space |

After normalisation, the text is lowercased, stripped, then SHA-256 hashed:

```python
# hashing.py — public API

def normalize_log(raw_log: str) -> str:
    text = raw_log.lower()
    for pattern, token in _RULES:
        text = pattern.sub(token, text)
    return text.strip()

def compute_log_hash(raw_log: str) -> str:
    """Returns 64-char lowercase SHA-256 hex digest."""
    normalized = normalize_log(raw_log)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

**Collision guarantee example:**

```python
a = "[2026-05-26 14:32:01] ERROR pid=1234 192.168.1.1 FATAL: conn exhausted"
b = "[2026-05-27 09:15:44] ERROR pid=9999 10.0.0.22  FATAL: conn exhausted"
assert compute_log_hash(a) == compute_log_hash(b)  # True
```

Both log lines normalise to:
```
[<timestamp>] error <id> <ip> fatal: conn exhausted
```
...and therefore produce the **same SHA-256 digest** — the cache treats them as identical incidents.

### 3.3 Updated LangGraph DAG — 13-Node Graph

Phase 2 expands the DAG from 11 to **13 nodes**, inserting `cache_check_node` and `cache_write_node` around the existing `classifier_node`:

```
START
  │
  ▼
┌──────────────────────────┐
│   workflow_start_node    │  Sets execution.status = RUNNING
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│    cache_check_node      │  ← NEW: hash lookup against active backend
└──────────────┬───────────┘
               │ conditional_edge: route_after_cache()
               │  ├── "cache_miss" → classifier_node
               │  └── "cache_hit"  → orchestrator_node  (LLM skipped)
               │
      ┌────────┴────────┐
      │                 │
      ▼ (miss)          ▼ (hit) ──────────────────────────────────┐
┌──────────────┐                                                  │
│ classifier_  │  → HTTP POST /generate → Colab API               │
│    node      │  → Returns ClassificationData                    │
└──────┬───────┘                                                  │
       │                                                          │
       ▼                                                          │
┌──────────────────────────┐                                      │
│    cache_write_node      │  ← NEW: persist to backend           │
└──────────────┬───────────┘    (guards: skip if None/Fallback)   │
               │                                                  │
               └─────────────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────┐
│    orchestrator_node     │  Severity = CRITICAL|FATAL → fast_track (P1)
└──────────────┬───────────┘  Else → standard_flow (P3)
               │
               ▼
┌──────────────────────────┐
│  human_in_the_loop_node  │  Approval gate (stub: auto-approves)
└──────────────┬───────────┘
               │ conditional_edge: route_after_human()
               │  ├── user_approved=True  → fan-out [rag_node, history_node]
               │  └── user_approved=False → END
               │
        ┌──────┴──────┐
        ▼             ▼
  ┌──────────┐  ┌──────────────┐
  │ rag_node │  │ history_node │  Run in PARALLEL (LangGraph fan-out)
  └────┬─────┘  └──────┬───────┘
       │               │
       └───────┬───────┘
               ▼
┌──────────────────────────┐
│   merge_context_node     │  Combines playbook_steps + historical_incidents
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│  diagnostic_agent_node   │  Returns DiagnosticAnalysis (root_cause, confidence)
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│   solution_agent_node    │  Returns SolutionAnalysis (List[CommandStep])
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│   risk_assessor_node     │  Regex scan for destructive patterns
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│  workflow_complete_node  │  Records processing_time_ms, status=COMPLETED
└──────────────┬───────────┘
               │
              END
```

**Graph wiring in `graph.py` (cache section):**

```python
# Cache layer edges — Phase 2 addition

workflow.add_edge("workflow_start_node", "cache_check_node")

workflow.add_conditional_edges(
    "cache_check_node",
    route_after_cache,            # returns "cache_hit" or "cache_miss"
    {
        "cache_miss": "classifier_node",
        "cache_hit":  "orchestrator_node",  # jumps over LLM entirely
    },
)

# Fresh classification path: classifier → cache_write → orchestrator
workflow.add_edge("classifier_node",  "cache_write_node")
workflow.add_edge("cache_write_node", "orchestrator_node")
```

### 3.4 State Architecture — Updated (`state.py`)

The `LogState` Pydantic model is unchanged in structure, but `duplicate_count` (previously tracking-only) is now **actively populated** by `cache_check_node` on a cache hit:

```
LogState
├── trace_id          : str (UUID auto-generated)
├── raw_log           : str (the incoming log line)
├── duplicate_count   : int  ← populated by cache_check_node on HIT
│
├── classification    : ClassificationData?
│   ├── category      : str
│   ├── source        : str
│   ├── severity      : SeverityLevel (Info | Warning | Critical | Fatal)
│   └── summary       : str
│
├── routing           : RoutingContext?
│   ├── route         : "skip" | "standard_flow" | "fast_track"
│   └── priority      : "P1" | "P2" | "P3" | "P4"
│
├── user_approved     : bool?
│
├── rag_result        : RagResult?
├── history_result    : HistoryResult?
├── merged_knowledge  : MergedKnowledge?
├── diagnostic        : DiagnosticAnalysis?
├── solution          : SolutionAnalysis?
├── security_check    : RiskAssessment?
│
└── execution         : ExecutionMetadata
    ├── workflow_status    : WorkflowStatus
    ├── retry_count        : int
    ├── processing_time_ms : int?
    ├── started_at         : datetime (UTC)
    └── completed_at       : datetime?
```

---

## 4. New Module Breakdown — Phase 2 Delta

### 4.1 `ai_core/cache/hashing.py`

**Role:** Pure, stateless log normalisation and SHA-256 hashing. No I/O, no side effects. Safe to import from any context (tests, notebooks, nodes).

**Design constraint:** The module is deliberately isolated — it has no imports from the rest of the `logpulse` codebase. This makes it independently testable and reusable outside LangGraph.

**Key functions:**

| Function | Signature | Purpose |
|---|---|---|
| `normalize_log` | `(raw_log: str) -> str` | Apply all 10 normalisation rules; return stable, lowercased, whitespace-collapsed string |
| `compute_log_hash` | `(raw_log: str) -> str` | `normalize_log` + `hashlib.sha256` → 64-char hex digest |

**Rule ordering is critical:** More specific patterns (full ISO 8601 timestamps) run before broader patterns (bare wall-clock times) to avoid partial consumption. Rules 7 and 8 (hex matching) are similarly ordered: `0x`-prefixed literals are consumed before the bare hex rule runs, preventing double-replacement.

**Hash properties:**

- **Deterministic:** identical semantic content always produces the same hash
- **Collision-resistant:** SHA-256 provides 2^256 address space; practical collision probability is negligible
- **Cross-session stable:** the normalisation rules are versioned in code; a rule change invalidates the entire cache (acceptable — the pipeline re-learns on miss)

---

### 4.2 `ai_core/cache/cache_manager.py`

**Role:** Backend-agnostic storage layer. Wraps any `CacheBackend` implementation and owns the hashing logic, so LangGraph nodes never handle raw hashes or serialisation.

#### Data Model: `CachedClassification`

```python
class CachedClassification(BaseModel):
    log_hash:       str           # SHA-256 of the normalised log (primary key)
    classification: Dict[str, Any] # Serialised ClassificationData
    hit_count:      int = 1       # Occurrence counter (starts at 1 on write)
    first_seen:     datetime      # UTC timestamp of first occurrence
    last_seen:      datetime      # UTC timestamp of most recent hit (updated on each hit)
```

> **Design constraint:** Only the `classification` dict is cached. Remediation plans (RAG results, diagnostic analysis, solution commands) are **never stored** in the cache. Each incident still gets fresh downstream reasoning even when the classification is served from cache.

#### Backend Hierarchy

```
CacheBackend (ABC)
├── InMemoryCacheBackend  — threading.Lock + dict  (default; dev/test)
└── SqliteCacheBackend    — SQLite WAL + UPSERT, thread-safe (production)
```

**`InMemoryCacheBackend`:**
- Process-lifetime only; cleared on restart
- Thread-safe via `threading.Lock`
- Zero dependencies beyond stdlib
- Default backend; requires no configuration

**`SqliteCacheBackend`:**
- Persistent across restarts — survives `main.py` restarts
- WAL journal mode allows concurrent reads without blocking writes
- Single-table schema (`classification_cache`), `log_hash` as `PRIMARY KEY`
- UPSERT semantics via `ON CONFLICT(log_hash) DO UPDATE`:

```sql
CREATE TABLE IF NOT EXISTS classification_cache (
    log_hash       TEXT    PRIMARY KEY,
    classification TEXT    NOT NULL,        -- JSON string
    hit_count      INTEGER NOT NULL DEFAULT 1,
    first_seen     TEXT    NOT NULL,        -- ISO-8601 UTC
    last_seen      TEXT    NOT NULL         -- ISO-8601 UTC
);
```

```sql
INSERT INTO classification_cache (log_hash, classification, hit_count, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(log_hash) DO UPDATE SET
    hit_count = excluded.hit_count,
    last_seen = excluded.last_seen;
```

#### `CacheManager` — High-Level Interface

```python
class CacheManager:
    def lookup(self, raw_log: str) -> Optional[CachedClassification]:
        # Hashes log internally; returns entry + bumps hit_count on hit; None on miss

    def store(self, raw_log: str, classification: Dict[str, Any]) -> None:
        # No-op if already present; lookup() owns hit_count updates

    def stats(self) -> CacheStats:
        # Returns CacheStats(total, hits, misses, hit_rate)
```

**`CacheStats`** is a frozen dataclass providing session-level observability:

```python
@dataclasses.dataclass(frozen=True)
class CacheStats:
    total:    int
    hits:     int
    misses:   int
    hit_rate: float   # 0.0–1.0; zero-division safe (returns 0.0 when total == 0)
```

**Extensibility guarantee:** The ABC interface (`get` / `set`) means Redis, Memcached, or PostgreSQL backends can be added with zero changes to `graph.py` or `cache_node.py`. Only the `configure_cache(backend)` call at startup changes.

---

### 4.3 `ai_core/cache/cache_node.py`

**Role:** The bridge between the cache layer and LangGraph. Exposes two node functions and one conditional edge router. Manages the module-level singleton `CacheManager` instance.

#### Module-Level Singleton

```python
_backend: CacheBackend = InMemoryCacheBackend()   # default; zero-config
_manager: CacheManager = CacheManager(_backend)
```

#### `configure_cache(backend)` — Startup Hook

```python
def configure_cache(backend: CacheBackend) -> None:
    global _backend, _manager
    _backend = backend
    _manager = CacheManager(backend)
```

Call this **once at application startup** (before invoking the LangGraph app) to switch to SQLite:

```python
from cache.cache_node import configure_cache
from cache.cache_manager import SqliteCacheBackend

configure_cache(SqliteCacheBackend("data/logpulse_cache.db"))
```

#### Node 1: `cache_check_node`

```python
def cache_check_node(state: LogState) -> Dict[str, Any]:
```

| Scenario | Action | Returns |
|---|---|---|
| **Cache hit** | Reconstructs `ClassificationData` via `normalize_severity`; sets `duplicate_count` to stored `hit_count` | `{"classification": <ClassificationData>, "duplicate_count": <int>}` |
| **Cache miss** | Prints miss message | `{}` (empty dict — `state.classification` remains `None`) |

**Important detail on hit:** `normalize_severity` is imported from `classifier_agent.py` to ensure the severity string stored as JSON (e.g., `"Fatal"`) is correctly reconstructed into the `SeverityLevel` enum expected by downstream nodes.

#### Node 2: `cache_write_node`

```python
def cache_write_node(state: LogState) -> Dict[str, Any]:
```

Write-only node — always returns `{}`. Two guards prevent cache poisoning:

| Guard | Condition | Action |
|---|---|---|
| **Null guard** | `state.classification is None` | Skip write — classifier failed silently |
| **Fallback guard** | `state.classification.source == "Fallback"` | Skip write — API was unreachable; a fallback classification must never be cached and served to future incidents |

When both guards pass, stores the classification via `CacheManager.store()` using `model_dump(mode="json")` to convert `SeverityLevel` enum to its string value before JSON serialisation.

#### Edge Router: `route_after_cache`

```python
def route_after_cache(state: LogState) -> str:
    if state.classification is not None:
        return "cache_hit"    # → orchestrator_node
    return "cache_miss"       # → classifier_node
```

The routing signal is the presence or absence of `state.classification`. A `cache_check_node` hit populates it; a miss leaves it `None`. This is the canonical conditional edge in Phase 2.

#### `get_cache_stats()`

```python
def get_cache_stats() -> CacheStats:
    return _manager.stats()
```

Call this from `main.py` or monitoring hooks to report session-level cache effectiveness after a run.

---

## 5. Updated End-to-End Data Flow

The following two scenarios trace the complete lifecycle for the Phase 2 graph, using the same mock log from Phase 1:

**Input log:**
```
[2026-05-26 14:32:01] ERROR [postgres-db-01] FATAL: remaining connection slots are reserved
for non-replication superuser connections
```

---

### Scenario A — Cache Miss (First Occurrence)

This is the first time this log pattern has been seen. The cache has no entry. The full LLM + downstream pipeline executes.

**Node 1 — `workflow_start_node`**
- Sets `execution.workflow_status = RUNNING`

**Node 2 — `cache_check_node`**
- Calls `CacheManager.lookup(state.raw_log)`
- `compute_log_hash` normalises the log:
  ```
  [<timestamp>] error [postgres-db-01] fatal: remaining connection slots are reserved for non-replication superuser connections
  ```
  → SHA-256 hash: `a3f7d1...` (64 hex chars)
- `InMemoryCacheBackend.get("a3f7d1...")` → `None`
- Prints: `[Cache] MISS — routing to classifier_node.`
- Returns `{}` — `state.classification` remains `None`

**Conditional edge `route_after_cache`**
- `state.classification is None` → returns `"cache_miss"` → routes to `classifier_node`

**Node 3 — `classifier_node`**
- Posts `{"prompt": "<raw_log>"}` to Colab API
- Colab runs Qwen2.5 inference; the model returns:
  ```json
  {
    "category": "DatabaseError",
    "source":   "PostgreSQL",
    "severity": "Fatal",
    "summary":  "Connection pool exhausted on postgres-db-01"
  }
  ```
- `normalize_severity("Fatal")` → `SeverityLevel.FATAL`
- `state.classification` is now populated

**Node 4 — `cache_write_node`**
- `state.classification` is not `None` ✓
- `state.classification.source` is `"PostgreSQL"`, not `"Fallback"` ✓
- Calls `CacheManager.store(raw_log, classification.model_dump(mode="json"))`
- `SqliteCacheBackend.set(CachedClassification(log_hash="a3f7d1...", hit_count=1, ...))`
- Prints: `[Cache] WRITE — stored classification for pattern (category=DatabaseError, severity=Fatal).`
- Returns `{}`

**Node 5 — `orchestrator_node`**
- `severity == SeverityLevel.FATAL` → `route="fast_track"`, `priority="P1"`

**Nodes 6–13 — remaining pipeline (unchanged from Phase 1)**
- `human_in_the_loop_node` → fan-out to `rag_node` + `history_node` (parallel)
- `merge_context_node` → `diagnostic_agent_node` → `solution_agent_node` → `risk_assessor_node` → `workflow_complete_node` → END

---

### Scenario B — Cache Hit (Duplicate Incident)

A second log line arrives, representing the same PostgreSQL connection exhaustion event but at a different timestamp, from a different replica IP:

```
[2026-05-26 14:32:02] ERROR [postgres-db-02] FATAL: remaining connection slots are reserved
for non-replication superuser connections
```

**Node 1 — `workflow_start_node`**
- Sets `execution.workflow_status = RUNNING` (new trace, new state)

**Node 2 — `cache_check_node`**
- `compute_log_hash` normalises the new log — identical normalised text as Scenario A:
  ```
  [<timestamp>] error [postgres-db-02] fatal: remaining connection slots are reserved for non-replication superuser connections
  ```

  > **Note:** `postgres-db-02` is not a timestamp, IP, UUID, or long integer, so it is *not* stripped. This is correct behaviour — the host suffix differs but the error pattern is still the same incident type. The classifier will correctly match on category + error message content regardless.

  → SHA-256 hash: `a3f7d1...` (same digest as Scenario A)
- `InMemoryCacheBackend.get("a3f7d1...")` → `CachedClassification(hit_count=1, ...)`
- `CacheManager.lookup()` bumps `hit_count` → 2, updates `last_seen`, persists via `backend.set()`
- Reconstructs `ClassificationData` from stored dict:
  ```python
  ClassificationData(
      category="DatabaseError",
      source="PostgreSQL",
      severity=SeverityLevel.FATAL,   # normalize_severity("Fatal") applied
      summary="Connection pool exhausted on postgres-db-01"
  )
  ```
- Sets `state.duplicate_count = 2`
- Prints: `[Cache] HIT — pattern seen 2x, skipping LLM call. (hash=a3f7d1c8e2f1…)`
- Returns `{"classification": <ClassificationData>, "duplicate_count": 2}`

**Conditional edge `route_after_cache`**
- `state.classification is not None` → returns `"cache_hit"` → routes **directly to `orchestrator_node`**
- `classifier_node` and `cache_write_node` are **never entered** — zero Colab API calls

**Node 3 — `orchestrator_node`** (directly, skipping 2 nodes)
- `severity == SeverityLevel.FATAL` → `route="fast_track"`, `priority="P1"`

**Nodes 4–11 — remaining pipeline executes normally**
- Fresh RAG retrieval, fresh diagnostic reasoning, fresh solution generation, fresh risk assessment
- The cache serves the *classification* only; every downstream intelligence step is re-run per incident

---

## 6. Updated File Map

| File | Phase 1 Status | Phase 2 Status | Notes |
|---|---|---|---|
| `ai_core/cache/hashing.py` | ❌ Didn't exist | ✅ **New** | Stateless; fully tested |
| `ai_core/cache/cache_manager.py` | ❌ Didn't exist | ✅ **New** | InMemory (default) + SQLite backends |
| `ai_core/cache/cache_node.py` | ❌ Didn't exist | ✅ **New** | LangGraph nodes + edge router |
| `ai_core/cache/__init__.py` | ❌ Didn't exist | ✅ **New** | Public surface of `cache` package |
| `ai_core/workflow/graph.py` | ✅ Complete (11 nodes) | ✅ **Updated** (13 nodes) | Cache wired; sys.path bootstrap added |
| `ai_core/workflow/state.py` | ✅ Complete | ✅ Unchanged | `duplicate_count` now actively used |
| `ai_core/workflow/agents/classifier_agent.py` | ✅ Complete | ✅ Unchanged | Update `COLAB_API_URL` each session |
| `ai_core/workflow/main.py` | ✅ Complete | ✅ Unchanged | Add `configure_cache()` call for SQLite |
| `data/playbooks_fixed.json` | ✅ Complete (102 entries) | ✅ Unchanged | Already indexed |
| `data/chroma_playbooks/` | ⚠️ Needs unzip | ⚠️ Pending | Unzip `chroma_playbooks.zip` for real RAG |
| `notebooks/test_model_inference.ipynb` | ✅ Complete | ✅ Unchanged | Token secured via Colab Secrets |
| `notebooks/build_vector_db.ipynb` | ✅ Complete (already run) | ✅ Unchanged | Re-run only if playbooks change |
| `backend/` | ❌ Empty placeholder | ❌ Empty placeholder | FastAPI backend — Phase 3 |
| `frontend/` | ❌ Empty placeholder | ❌ Empty placeholder | Dashboard — Phase 3 |

---

## 7. Next Steps & Phase 3 Roadmap

### 7.1 Developer Setup — Starting a Phase 2 Session

Follow these steps at the beginning of every development session.

**Step 1 — Start the Colab inference backend:**

> Open `notebooks/test_model_inference.ipynb` in Google Colab and run all cells. Copy the printed ngrok URL.

**Step 2 — Update the ngrok URL:**

```python
# ai_core/workflow/agents/classifier_agent.py (line 21)
COLAB_API_URL = "https://<YOUR-NEW-NGROK-URL>/generate"
```

**Step 3 — (Optional but recommended) Switch to SQLite for persistence:**

Add to `ai_core/workflow/main.py` before the pipeline runs:

```python
from cache.cache_node import configure_cache
from cache.cache_manager import SqliteCacheBackend

configure_cache(SqliteCacheBackend("data/logpulse_cache.db"))
```

**Step 4 — Run the local pipeline:**

```bash
cd ai_core/workflow
python main.py
```

**Step 5 — Inspect cache stats after the run:**

```python
from cache.cache_node import get_cache_stats
stats = get_cache_stats()
print(f"Hit rate: {stats.hit_rate:.1%}  ({stats.hits}/{stats.total})")
```

---

### 7.2 Remaining Stub Nodes — Phase 3 Priority Queue

The following nodes remain hardcoded stubs. Phase 3 must replace each with a real implementation.

#### Node A — `rag_node` (Priority: **High**)

**Current stub:**
```python
def rag_node(state: LogState) -> Dict[str, Any]:
    return {"rag_result": RagResult(playbook_steps="Increase PostgreSQL...", rag_confidence=0.95)}
```

**Phase 3 plan:** The ChromaDB vector store is pre-built. Unzip `chroma_playbooks.zip` to `data/chroma_playbooks/` and wire it:

```python
import chromadb, json

client = chromadb.PersistentClient(path="data/chroma_playbooks")
collection = client.get_collection("playbooks")

def rag_node(state: LogState) -> Dict[str, Any]:
    query = f"{state.classification.category} {state.raw_log}"
    results = collection.query(query_texts=[query], n_results=3)
    top = results["metadatas"][0][0]
    steps = json.loads(top["resolution_steps"])
    confidence = 1.0 - results["distances"][0][0]
    return {"rag_result": RagResult(
        playbook_steps="\n".join(steps),
        rag_confidence=round(confidence, 4)
    )}
```

**Embedding model:** `all-MiniLM-L6-v2` (via ONNX, ChromaDB default). **Distance metric:** cosine. **Collection name:** `playbooks`.

---

#### Node B — `history_node` (Priority: **Medium**)

**Current stub:** Returns hardcoded `"Incident #892 resolved by scaling database connection pools."`

**Phase 3 plan:** Query a local SQLite incident history seeded from `playbooks_fixed.json`. Filter by `state.classification.category` and `state.classification.source`; return 3 most recent similar incidents as a formatted text summary.

---

#### Node C — `diagnostic_agent_node` (Priority: **High**)

**Current stub:** Returns hardcoded root cause and confidence score.

**Phase 3 plan:** Make a second Colab API call with a structured prompt built from `merged_knowledge` + `classification`. The `classifier_node` HTTP pattern can be cloned directly. This is also the strongest candidate for a **second LoRA fine-tuning run** — training the model to produce structured `{"root_cause": "...", "confidence_score": 0.0}` JSON from a combined context prompt.

---

#### Node D — `solution_agent_node` (Priority: **Medium**)

**Current stub:** Returns fixed two-step PostgreSQL remediation.

**Phase 3 plan:** Take `state.diagnostic.root_cause` + `state.merged_knowledge.playbook_steps` and use template-based generation over playbook `resolution_steps`. Introduce `CommandStep.destructive = True` for commands that modify persistent state (files, databases, running services) before introducing fully LLM-generated commands.

---

#### Node E — `human_in_the_loop_node` (Priority: **High for production**)

**Current stub:** Auto-approves (`return {"user_approved": True}`).

**Phase 3 plan:**
- **Minimal:** Write pending state to SQLite; poll for `user_approved` flag set by a companion CLI tool.
- **Target:** POST to a Slack webhook with a one-click approve/reject link. The `WorkflowStatus.WAITING_FOR_APPROVAL` enum is already defined in `state.py` — this node should set it before pausing.

---

### 7.3 Phase 3 New Deliverables

| Deliverable | Description | Priority |
|---|---|---|
| Real RAG integration | Wire `chroma_playbooks/` into `rag_node` | High |
| SQLite incident history | Seed from `playbooks_fixed.json`; wire into `history_node` | Medium |
| Live diagnostic reasoning | Second Colab API call in `diagnostic_agent_node` | High |
| Playbook-templated solutions | Template-based `solution_agent_node` using `resolution_steps` | Medium |
| Real human approval channel | Slack webhook or CLI polling for `human_in_the_loop_node` | High (production) |
| FastAPI backend | REST API wrapper around `logpulse_app.invoke()` in `backend/` | High |
| Frontend dashboard | Incident viewer, approval UI, cache hit-rate chart in `frontend/` | Medium |
| Multi-model orchestration | Route `diagnostic_agent_node` and `solution_agent_node` to separate fine-tuned adapters | Low (research) |
| Cache TTL / eviction policy | Expire SQLite entries after N days; add LRU eviction to InMemory backend | Low |
| Redis backend | Implement `CacheBackend` for Redis to support multi-process / distributed deployments | Low |

---

### 7.4 Architecture Target — Phase 3

```
                         ┌─────────────────────┐
  Raw log stream ──────► │   FastAPI Backend   │ ◄── (Phase 3 new)
                         └────────┬────────────┘
                                  │ LogState
                                  ▼
                         ┌─────────────────────┐
                         │  LangGraph Pipeline  │
                         │                     │
                         │  ┌───────────────┐  │
                         │  │ Cache Layer   │  │ ◄── Phase 2 ✅
                         │  └───────────────┘  │
                         │  ┌───────────────┐  │
                         │  │ Classifier    │──┼──► Colab Qwen2.5 API
                         │  │ (LLM)         │  │
                         │  └───────────────┘  │
                         │  ┌───────────────┐  │
                         │  │ Real RAG      │──┼──► ChromaDB (local)
                         │  └───────────────┘  │
                         │  ┌───────────────┐  │
                         │  │ Diagnostic    │──┼──► Colab API (2nd call)
                         │  │ Agent (LLM)   │  │
                         │  └───────────────┘  │
                         │  ┌───────────────┐  │
                         │  │ Risk Assessor │  │
                         │  └───────────────┘  │
                         └────────┬────────────┘
                                  │
                         ┌────────▼────────────┐
                         │  Frontend Dashboard  │ ◄── (Phase 3 new)
                         │  + Slack Approval    │
                         └─────────────────────┘
```

---

*Document generated from source code analysis of `ai_core/cache/` (Phase 2 additions) and `ai_core/workflow/graph.py` (updated wiring). Evolves from Phase 1 commit `2dfdf7a` on branch `abdulaziz`.*
