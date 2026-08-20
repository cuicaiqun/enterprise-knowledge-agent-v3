# AGENTS.md

## Repository layout

| Path | Purpose |
|------|---------|
| `project/code/python/` | Main FastAPI + LangGraph backend (run tests here) |
| `project/code/` | Docker Compose stack (Neo4j, Chroma, Postgres, Kafka) |
| `project/ROADMAP.md` | P0/P1 acceptance tracker — source of truth for progress |
| `.cursor/skills/fullstack-dev-orchestrator/` | Orchestrator skill for autonomous iteration |

## Cursor Cloud specific instructions

### Default verification (no external services)

Unit tests mirror CI and do **not** require Docker or live LLM keys.
Cloud Builds use `requirements-cloud.txt` (no torch/sentence-transformers) to stay within VM limits; set `DISABLE_LOCAL_EMBEDDINGS=1`.

```bash
cd project/code/python
export REQUIRE_OPENAI_API_KEY=false
export UPDATE_MODE=off
export AUTH_ENABLED=true
export DISABLE_LOCAL_EMBEDDINGS=1
export REQUIRE_STRONG_SECRETS=false
bash scripts/run_unit_tests.sh
```

Deploy / secrets gate:

```bash
cd project/code/python
python scripts/check_p0_3_deploy.py
```

Orchestrator progress scan (repo root):

```bash
python .cursor/skills/fullstack-dev-orchestrator/scripts/assess_progress.py
```

### Secrets (configure in Cursor Dashboard → Cloud Agents → Secrets)

Add these for full-stack / E2E work (optional for unit tests):

| Secret | Purpose |
|--------|---------|
| `OPENAI_API_KEY` | LLM calls (ingest / QA agents) |
| `OPENAI_BASE_URL` | Compatible API endpoint (e.g. DeepSeek) |
| `OPENAI_MODEL` | Model id |
| `NEO4J_PASSWORD` | Docker Compose Neo4j auth |
| `POSTGRES_PASSWORD` | Postgres / state store |
| `JWT_SECRET` | Auth tokens (production-like runs) |

After secrets are set, copy or merge into `project/code/python/.env` before starting Compose.

### Docker build on hosts with Clash/Mihomo TUN

`apt`/`pip` during **image build** may fail (`deb.debian.org` → `198.18.0.x` fake-IP). **Runtime/production does not run apt** — ship prebuilt images from CI or a clean builder.

Local escapes: `HTTP_PROXY=http://127.0.0.1:<clash-mixed-port>` for build, or `INSTALL_SYSTEM_OCR=0` to skip OCR system packages. See `project/docs/MVP_demo_guide.md`.

### Optional: local dependency stack

From `project/code/` (needs `.env` with passwords):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file python/.env up -d
```

Then run optional E2E scripts from `project/code/python/scripts/` (e.g. `e2e_neo4j_readonly.sh`).

### Working conventions

- Real product code lives under `project/` — do not write business logic into `skills/` or `templates/`.
- Prefer one thin ROADMAP slice per agent run; attach test evidence when updating ROADMAP status.
- Never commit `.env`, keys, or TLS material.
