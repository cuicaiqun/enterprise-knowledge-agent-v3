#!/usr/bin/env bash
# M4: live HTTP path login → upload → ingest task → QA.
# Requires API already up (compose or uvicorn) plus LLM credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export RUN_MVP_E2E=1
export DISABLE_LOCAL_EMBEDDINGS="${DISABLE_LOCAL_EMBEDDINGS:-1}"
export UPDATE_MODE="${UPDATE_MODE:-off}"
export WALL_TIMEOUT="${WALL_TIMEOUT:-180}"
export MVP_API_BASE="${MVP_API_BASE:-http://127.0.0.1:8080}"

if [[ -z "${ADMIN_PASS:-}" ]]; then
  echo "SKIP live MVP E2E: set ADMIN_PASS" >&2
  exit 0
fi

if ! curl -fsS --max-time 5 "${MVP_API_BASE}/api/health" >/tmp/mvp-health.json; then
  echo "SKIP live MVP E2E: ${MVP_API_BASE}/api/health unreachable" >&2
  exit 0
fi

echo "=== M4 live MVP main path (${MVP_API_BASE}) ==="
exec bash scripts/run_unit_tests.sh tests/test_mvp_main_path_e2e.py -vv "$@"
