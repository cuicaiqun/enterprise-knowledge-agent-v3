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
        probe = vector_store.refresh_embeddings_probe()
        logger.info("worker embeddings_probe=%s", probe)
        if probe.get("status") != "ok" and not vector_store.embeddings_available:
            logger.error(
                "worker embeddings unavailable — ingest jobs will fail. hint=%s",
                probe.get("hint") or probe.get("error"),
            )
    except Exception:
        logger.exception("worker embeddings probe failed")
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
    await _refresh_worker_heartbeat(ctx)
    logger.info("arq ingest worker ready")


async def _refresh_worker_heartbeat(ctx: dict) -> None:
    """写入 Redis 心跳，供 API /api/health 判断 worker 是否存活。"""
    import time

    from services.ingest_queue import ArqIngestQueue

    redis_url = (settings.redis_url or "redis://localhost:6379/0").strip()
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        redis = await create_pool(RedisSettings.from_dsn(redis_url))
        await redis.set(ArqIngestQueue.HEARTBEAT_KEY, str(time.time()), ex=180)
        ctx["heartbeat_redis"] = redis
    except Exception:
        logger.exception("failed to write ingest worker heartbeat")


async def on_job_start(ctx: dict) -> None:
    """每个 job 前刷新心跳，避免长期 queued 无告警。"""
    import time

    from services.ingest_queue import ArqIngestQueue

    redis = ctx.get("heartbeat_redis")
    if redis is None:
        return
    try:
        await redis.set(ArqIngestQueue.HEARTBEAT_KEY, str(time.time()), ex=180)
    except Exception:
        logger.warning("ingest worker heartbeat refresh failed", exc_info=True)


async def shutdown(ctx: dict) -> None:
    redis = ctx.get("heartbeat_redis")
    if redis is not None:
        try:
            await redis.close(close_connection_pool=True)
        except Exception:
            logger.warning("heartbeat redis close failed", exc_info=True)
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
    on_job_start = on_job_start
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
