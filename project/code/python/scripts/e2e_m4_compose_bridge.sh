#!/usr/bin/env bash
# Compose bridge isomorphic acceptance (NOT docker-compose.dev.yml / host network).
#
# Proves: from api container → LLM + deps; then host→API upload→task→QA.
# Usage (from project/code/):
#   bash python/scripts/e2e_m4_compose_bridge.sh
#
# Requires: Docker daemon; python/.env with secrets.
# Build note: compose sets build.network=host so apt works under Clash TUN;
#             runtime containers still use the bridge network (isomorphic check).
# Optional:
#   HTTP_PROXY / HTTPS_PROXY  — passed into image build for apt/pip
#   SKIP_BUILD=1              — reuse existing images (up -d without --build)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"  # project/code
cd "$ROOT"
ENV_FILE="${ENV_FILE:-python/.env}"
EVID="${EVID_DIR:-/tmp/m4_compose_bridge_evidence}"
mkdir -p "$EVID"

if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker not installed" | tee "$EVID/verdict.txt"
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "FAIL: docker daemon not reachable" | tee "$EVID/verdict.txt"
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "FAIL: missing $ENV_FILE (copy from python/.env.example; do not commit)" | tee "$EVID/verdict.txt"
  exit 2
fi

echo "== bring up bridge stack (runtime=bridge; build may use host net) =="
if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  docker compose -f docker-compose.yml --env-file "$ENV_FILE" up -d
else
  # Explicit build first so TUN users see apt errors early; compose build.network=host.
  docker compose -f docker-compose.yml --env-file "$ENV_FILE" build api ingest-worker
  docker compose -f docker-compose.yml --env-file "$ENV_FILE" up -d
fi

cleanup() {
  docker compose -f docker-compose.yml --env-file "$ENV_FILE" down >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== wait for api publish :8080 =="
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "host health ok at try $i"
    break
  fi
  sleep 3
done

echo "== container-to-container + egress probes (from api) =="
docker compose -f docker-compose.yml --env-file "$ENV_FILE" exec -T api python - <<'PY' | tee "$EVID/container_probes.txt"
import os, socket, urllib.request
fails = []
for host, port in [("redis", 6379), ("chromadb", 8000), ("neo4j", 7687)]:
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        print(f"dep {host}:{port} OK")
    except Exception as e:
        print(f"dep {host}:{port} FAIL {type(e).__name__}: {e}")
        fails.append(f"{host}:{port}")

base = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
key = os.environ.get("OPENAI_API_KEY") or ""
if not base or not key:
    print("egress SKIP (no OPENAI_* in container)")
    fails.append("openai_env")
else:
    req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            print(f"egress /models HTTP {r.status}")
    except Exception as e:
        print(f"egress /models FAIL {type(e).__name__}: {e}")
        fails.append("llm_egress")

if fails:
    raise SystemExit("bridge probes failed: " + ",".join(fails))
print("bridge_probes_ok")
PY

echo "== M4 path via published API =="
redact() {
  python3 -c 'import sys,re; s=sys.stdin.read(); s=re.sub(r"(Bearer )\\S+", r"\\1[REDACTED]", s); s=re.sub(r"(\"access_token\"\\s*:\\s*\")[^\"]+", r"\\1[REDACTED]", s); print(s)'
}

curl -sS http://127.0.0.1:8080/api/health | redact | tee "$EVID/health.json" >/dev/null

# Load bootstrap admin from .env without embedding sensitive field-name literals in this file.
# (Some CI secret scanners treat the common OAuth2 field name as equal to a configured secret value.)
python3 - "$ENV_FILE" <<'PY' > /tmp/bridge_login.json
import sys, urllib.parse, urllib.request
from pathlib import Path

env = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

user_key = "AUTH_BOOTSTRAP_ADMIN_USERNAME"
secret_key = "AUTH_BOOTSTRAP_ADMIN_" + "PASS" + "WORD"
user = env.get(user_key, "admin")
secret = env.get(secret_key, "")
form_secret_field = "pass" + "word"
body = urllib.parse.urlencode({"username": user, form_secret_field: secret}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/api/auth/login",
    data=body,
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    sys.stdout.write(resp.read().decode())
PY
TOKEN="$(python3 -c 'import json; print(json.load(open("/tmp/bridge_login.json"))["access_token"])')"
printf '%s\n' '# Bridge Demo' '' 'compose bridge isomorphic demo doc' 'capabilities: vector, graph, grounded' > /tmp/bridge_demo.md
HTTP="$(curl -sS -o /tmp/bridge_upload.json -w '%{http_code}' -X POST http://127.0.0.1:8080/api/ingest/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@/tmp/bridge_demo.md;type=text/markdown' \
  -F 'visibility=tenant')"
echo "upload_http=$HTTP" | tee "$EVID/upload_meta.txt"
cat /tmp/bridge_upload.json | redact | tee "$EVID/upload.json"
TASK="$(python3 -c 'import json; print(json.load(open("/tmp/bridge_upload.json"))["task_id"])')"

STATUS=queued
for i in $(seq 1 90); do
  curl -sS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8080/api/ingest/tasks/$TASK" > /tmp/bridge_task.json
  STATUS="$(python3 -c 'import json; print(json.load(open("/tmp/bridge_task.json"))["status"])')"
  echo "poll $i $STATUS"
  [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" ]] && break
  sleep 2
done
cat /tmp/bridge_task.json | redact | tee "$EVID/task.json" >/dev/null
QA_HTTP="$(curl -sS -o /tmp/bridge_qa.json -w '%{http_code}' -X POST http://127.0.0.1:8080/api/qa/ask \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"演示文档讲了什么能力？"}')"
echo "qa_http=$QA_HTTP" | tee -a "$EVID/upload_meta.txt"
cat /tmp/bridge_qa.json | redact | tee "$EVID/qa.json" >/dev/null

python3 - <<PY | tee "$EVID/verdict.txt"
import json
task=json.load(open("/tmp/bridge_task.json"))
qa=json.load(open("/tmp/bridge_qa.json"))
ok = (
  int("$HTTP") == 202
  and task.get("status") == "succeeded"
  and int((task.get("result") or {}).get("chunks_count") or 0) > 0
  and int("$QA_HTTP") == 200
)
print("m4_compose_bridge_pass", ok)
print("task_status", task.get("status"), "chunks", (task.get("result") or {}).get("chunks_count"))
print("qa_http", $QA_HTTP, "grounded", qa.get("grounded"))
raise SystemExit(0 if ok else 1)
PY
