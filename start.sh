#!/bin/bash
# LogPulse AI — Start both API and frontend
# Usage: ./start.sh
# Requires: Python env with dependencies installed, Node.js

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║        LogPulse AI — Starting Up         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Load .env if present ──────────────────────────────────────────────────────
if [ -f "$PROJECT_ROOT/.env" ]; then
  echo "[Config] Loading .env"
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

# ── Terminal 1: FastAPI on port 8000 ─────────────────────────────────────────
echo "[API]      Starting FastAPI on http://localhost:8000"
cd "$PROJECT_ROOT"
uvicorn api.main:app --reload --port 8000 --host 0.0.0.0 &
API_PID=$!
echo "[API]      PID $API_PID"

# ── Terminal 2: Next.js frontend on port 3000 ─────────────────────────────────
echo "[Frontend] Starting Next.js on http://localhost:3000"
cd "$PROJECT_ROOT/frontend"
npm run dev &
FRONTEND_PID=$!
echo "[Frontend] PID $FRONTEND_PID"

echo ""
echo "  API:      http://localhost:8000/api/health"
echo "  Docs:     http://localhost:8000/docs"
echo "  Frontend: http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both services."
echo ""

# ── Wait and cleanup on Ctrl+C ───────────────────────────────────────────────
trap "echo ''; echo 'Stopping...'; kill $API_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM

wait $API_PID $FRONTEND_PID
