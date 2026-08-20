"""arq worker 入口：python -m workers.ingest_worker"""

from __future__ import annotations

import logging

from arq.connections import RedisSettings

from config import settings
from config.secrets_guard import enforce_secrets_or_raise
from observability.logging_config import setup_logging
from services.ingest_runner import arq_process_ingest_job, bind_ingest_runtime
from services.ingest_jobs import create_ingest_job_store
from services.knowledge_graph import KnowledgeGraphService
from services.state_store import create_state_store
from services.vector_store import VectorStoreService
from orchestrator.graph import build_knowledge_graph_workflow
from agents.doc_parser_agent import DocParserAgent
from agents.knowledge_extract_agent import KnowledgeExtractAgent
from agents.knowledge_update_agent import KnowledgeUpdateAgent

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    setup_logging(settings.log_level)
    enforce_secrets_or_raise(settings)
    state_store = create_state_store()
    job_store = create_ingest_job_store()
    vector_store = VectorStoreService()
    knowledge_graph = KnowledgeGraphService()
    try:
        await vector_store.init()
    except Exception:
        logger.exception("worker vector_store init failed")
    try:
        await knowledge_graph.init()
    except Exception:
        logger.exception("worker knowledge_graph init failed")
    update_agent = KnowledgeUpdateAgent(
        doc_parser=DocParserAgent(),
        knowledge_extractor=KnowledgeExtractAgent(),
        vector_store=vector_store,
        knowledge_graph=knowledge_graph,
        state_store=state_store,
    )
    # Ingest worker only needs ingest/update pipelines — skip QA (avoids checkpointer deps).
    workflows = build_knowledge_graph_workflow(
        vector_store=vector_store,
        knowledge_graph=knowledge_graph,
        update_agent=update_agent,
        pipelines=("ingest", "update"),
    )
    bind_ingest_runtime(job_store=job_store, state_store=state_store, workflows=workflows)
    ctx["state_store"] = state_store
    ctx["job_store"] = job_store
    ctx["vector_store"] = vector_store
    ctx["knowledge_graph"] = knowledge_graph
    logger.info("arq ingest worker ready")


async def shutdown(ctx: dict) -> None:
    kg = ctx.get("knowledge_graph")
    if kg is not None:
        await kg.close()
    js = ctx.get("job_store")
    if js is not None:
        js.close()
    ss = ctx.get("state_store")
    if ss is not None:
        ss.close()


class WorkerSettings:
    functions = [arq_process_ingest_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(
        (settings.redis_url or "redis://localhost:6379/0").strip()
    )
    max_jobs = max(1, settings.ingest_workers)


if __name__ == "__main__":
    # arq workers.ingest_worker.WorkerSettings
    import sys
    from arq.cli import cli

    sys.argv = ["arq", "workers.ingest_worker.WorkerSettings"]
    cli()
