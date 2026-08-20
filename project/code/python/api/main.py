"""
FastAPI 入口 — 企业知识管理系统 REST API

提供三组接口:
  1. /api/ingest   — 文档上传 & 入库
  2. /api/qa       — 智能问答
  3. /api/admin    — 管理（统计、更新触发）
  4. /api/auth     — JWT 登录与用户管理
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.doc_parser_agent import DocParserAgent
from agents.knowledge_extract_agent import KnowledgeExtractAgent
from agents.knowledge_update_agent import ChangeType, DocumentChange, KnowledgeUpdateAgent
from api.auth.deps import get_current_user, require_admin, require_writer
from api.auth.models import DocumentACL, User
from api.auth.router import router as auth_router
from api.auth.store import auth_store
from config import settings
from config.secrets_guard import enforce_secrets_or_raise
from observability.logging_config import setup_logging
from observability.llm import LlmNotConfiguredError, ensure_llm_ready
from services.embeddings_ready import EmbeddingsNotReadyError, ensure_embeddings_ready
from services.qa_checkpoint import close_qa_checkpointer, init_qa_checkpointer

from observability.metrics import metrics_payload, set_dependency_up
from observability.middleware import RequestContextMiddleware
from observability.rate_limit import SlidingWindowRateLimiter
from orchestrator.graph import build_knowledge_graph_workflow
from services.cdc_processor import CDCProcessor
from services.ingest_jobs import IngestJob, IngestJobStore, create_ingest_job_store, new_job_id
from services.ingest_queue import IngestQueue, create_ingest_queue
from services.ingest_runner import bind_ingest_runtime, process_ingest_job
from services.knowledge_graph import KnowledgeGraphService
from services.state_store import (
    DocumentIndexStatus,
    IdempotencyStatus,
    MemoryStateStore,
    StateStore,
    create_state_store,
)
from services.upload_security import UploadSecurityError, save_upload_securely
from services.vector_store import VectorStoreService

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

vector_store = VectorStoreService()
knowledge_graph = KnowledgeGraphService()
state_store: StateStore = MemoryStateStore()
ingest_job_store: IngestJobStore | None = None
ingest_queue: IngestQueue | None = None
workflows: dict[str, Any] = {}
update_agent: KnowledgeUpdateAgent | None = None
cdc_task: asyncio.Task | None = None
_qa_rate_limiter = SlidingWindowRateLimiter(
    limit=settings.rate_limit_qa_per_minute,
    window_seconds=60.0,
)
_qa_tenant_rate_limiter = SlidingWindowRateLimiter(
    limit=settings.rate_limit_qa_per_tenant_per_minute,
    window_seconds=60.0,
)
_startup_status: dict[str, Any] = {
    "vector_store": "pending",
    "knowledge_graph": "pending",
    "state_store": "pending",
    "ingest_queue": "pending",
    "qa_checkpoint": "pending",
    "embeddings": "pending",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global update_agent, cdc_task, state_store, ingest_job_store, ingest_queue

    # P0-3：生产环境拒绝弱密钥启动
    enforce_secrets_or_raise(settings)

    os.makedirs(settings.upload_dir, exist_ok=True)
    auth_store.init()
    await init_qa_checkpointer()
    _startup_status["qa_checkpoint"] = settings.qa_checkpoint_backend
    state_store = create_state_store()
    _startup_status["state_store"] = getattr(state_store, "backend", "unknown")
    set_dependency_up("state_store", _startup_status["state_store"] in {"postgres", "memory"})
    vector_store.set_doc_searchable_checker(state_store.document_search_gate)
    knowledge_graph.set_doc_searchable_checker(state_store.document_search_gate)
    ingest_job_store = create_ingest_job_store()
    try:
        await vector_store.init()
        _startup_status["vector_store"] = "ok" if vector_store._store is not None else "uninitialized"
    except Exception:
        _startup_status["vector_store"] = "failed"
        logger.exception("Vector store init failed")
    set_dependency_up("vector_store", _startup_status["vector_store"] == "ok")
    try:
        emb_probe = vector_store.refresh_embeddings_probe()
        _startup_status["embeddings"] = emb_probe
        set_dependency_up("embeddings", emb_probe.get("status") == "ok" or vector_store.embeddings_available)
        if emb_probe.get("status") != "ok" and not vector_store.embeddings_available:
            logger.error(
                "embeddings unavailable at startup — ingest will be rejected. probe=%s",
                emb_probe,
            )
        else:
            logger.info("embeddings_probe=%s", emb_probe)
    except Exception as exc:
        _startup_status["embeddings"] = {"status": "unavailable", "error": str(exc)}
        set_dependency_up("embeddings", False)
        logger.exception("embeddings probe failed")
    try:
        await knowledge_graph.init()
        _startup_status["knowledge_graph"] = "ok"
    except Exception:
        _startup_status["knowledge_graph"] = "failed"
        logger.exception("Knowledge graph init failed")
    set_dependency_up("knowledge_graph", _startup_status["knowledge_graph"] == "ok")
    logger.info("startup_status=%s", _startup_status)
    update_agent = KnowledgeUpdateAgent(
        doc_parser=DocParserAgent(),
        knowledge_extractor=KnowledgeExtractAgent(),
        vector_store=vector_store,
        knowledge_graph=knowledge_graph,
        state_store=state_store,
    )
    workflows.update(build_knowledge_graph_workflow(
        vector_store=vector_store,
        knowledge_graph=knowledge_graph,
        update_agent=update_agent,
    ))

    bind_ingest_runtime(
        job_store=ingest_job_store,
        state_store=state_store,
        workflows=workflows,
    )
    try:
        ingest_queue = await create_ingest_queue(process_ingest_job)
        _startup_status["ingest_queue"] = getattr(ingest_queue, "backend", "unknown")
        set_dependency_up("ingest_queue", True)
    except Exception:
        _startup_status["ingest_queue"] = "failed"
        set_dependency_up("ingest_queue", False)
        logger.exception("Ingest queue init failed")
        # 本地降级，避免 API 无法启动
        from services.ingest_queue import LocalIngestQueue

        ingest_queue = LocalIngestQueue(process_ingest_job, concurrency=settings.ingest_workers)
        await ingest_queue.start()
        _startup_status["ingest_queue"] = "local_fallback"

    # Kafka 仅走 CDCProcessor；Watchdog 由 UpdateAgent 监听后调用同一 process_change
    mode = settings.update_mode.lower()
    watch_dir = settings.update_watch_directory or settings.upload_dir
    if mode == "watchdog":
        os.makedirs(watch_dir, exist_ok=True)
        update_agent.start_watching(watch_dir, asyncio.get_running_loop())
    elif mode == "kafka":
        cdc_task = asyncio.create_task(
            CDCProcessor(update_agent, state_store=state_store).start_kafka_consumer()
        )
    elif mode != "off":
        raise ValueError("UPDATE_MODE must be watchdog, kafka, or off")

    try:
        yield
    finally:
        if mode == "watchdog":
            update_agent.stop_watching()
        if cdc_task is not None:
            cdc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cdc_task
            cdc_task = None
        if ingest_queue is not None:
            await ingest_queue.stop()
            ingest_queue = None
        if ingest_job_store is not None:
            ingest_job_store.close()
            ingest_job_store = None
        state_store.close()
        await knowledge_graph.close()
        await close_qa_checkpointer()


app = FastAPI(
    title="com_agent_chat — 多Agent知识调度管理系统",
    description="支持多模态RAG、知识图谱、增量更新的企业级知识管理 API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.include_router(auth_router)

# ── Static Files & Frontend ──────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_frontend():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(static_dir, "index.html"))


# ── Request / Response Models ────────────────────────────────

class QuestionRequest(BaseModel):
    question: str
    session_id: str | None = None
    history: list[dict[str, str]] | None = None


class QuestionResponse(BaseModel):
    question: str
    answer: str
    confidence: float
    intent: str
    sources: list[dict[str, Any]]
    reasoning_steps: list[str]
    session_id: str = ""
    resolved_question: str = ""
    grounded: bool = True
    grounding_notes: list[str] = []


class IngestResponse(BaseModel):
    file_name: str
    chunks_count: int
    entities_count: int
    relations_count: int
    status: str
    doc_id: str = ""
    visibility: str = "tenant"
    idempotent_replay: bool = False
    task_id: str = ""


class IngestTaskAccepted(BaseModel):
    task_id: str
    status: str = "queued"
    file_name: str
    doc_id: str = ""
    visibility: str = "tenant"
    message: str = "accepted"


class IngestTaskStatus(BaseModel):
    task_id: str
    status: str
    file_name: str
    doc_id: str = ""
    visibility: str = "tenant"
    error: str = ""
    result: dict[str, Any] | None = None
    created_at: float = 0
    updated_at: float = 0
    started_at: float | None = None
    finished_at: float | None = None


class StatsResponse(BaseModel):
    vector_store: dict[str, Any]
    knowledge_graph: dict[str, Any]
    state_store: dict[str, Any] = {}


class UpdateRequest(BaseModel):
    file_path: str
    change_type: str = "modified"


class UpdateResponse(BaseModel):
    file_path: str
    vectors_added: int
    vectors_deleted: int
    entities_added: int
    relations_added: int
    success: bool
    processing_time_ms: float


# ── Ingest Endpoints ─────────────────────────────────────────

@app.post("/api/ingest/upload", tags=["文档入库"])
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    visibility: str = Form("tenant"),
    user: User = Depends(require_writer),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """上传文档并入库。默认异步：立即 202 + task_id；INGEST_ASYNC=false 时同步返回结果。

    幂等：优先使用 Idempotency-Key；否则按 doc_id + 内容哈希去重。
    """
    if visibility not in {"private", "tenant", "public"}:
        raise HTTPException(status_code=400, detail="visibility must be private|tenant|public")

    try:
        ensure_llm_ready()
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        ensure_embeddings_ready(vector_store)
    except EmbeddingsNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        saved = save_upload_securely(file)
    except UploadSecurityError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("secure upload failed")
        raise HTTPException(status_code=400, detail=f"Upload rejected: {exc}") from exc

    save_path = saved.stored_path
    display_name = saved.display_name

    # P1-1：抑制 watchdog，避免 API 入库与文件监听双跑
    if update_agent is not None:
        update_agent.suppress_watch(save_path)

    ingest_wf = workflows.get("ingest")
    if not ingest_wf:
        raise HTTPException(status_code=503, detail="Ingest workflow not initialized")

    doc_id = DocParserAgent._make_doc_id(save_path)
    content_hash = KnowledgeUpdateAgent._compute_hash(save_path)
    request_hash = hashlib.sha256(
        f"{doc_id}:{content_hash}:{visibility}:{user.tenant_id}".encode()
    ).hexdigest()
    key = (idempotency_key or "").strip() or f"ingest:{doc_id}:{content_hash}"

    begin = state_store.begin_idempotent(key, scope="ingest", request_hash=request_hash)
    if begin.status == IdempotencyStatus.REPLAY and begin.cached_response:
        cached = dict(begin.cached_response)
        cached["idempotent_replay"] = True
        # 已完成任务：直接 200 回放；若缓存仅有 task_id 则引导查询状态
        if cached.get("status") in {"success", "failed"} or "chunks_count" in cached:
            return IngestResponse(**{
                "file_name": cached.get("file_name", display_name),
                "chunks_count": int(cached.get("chunks_count") or 0),
                "entities_count": int(cached.get("entities_count") or 0),
                "relations_count": int(cached.get("relations_count") or 0),
                "status": cached.get("status") or "success",
                "doc_id": cached.get("doc_id") or doc_id,
                "visibility": cached.get("visibility") or visibility,
                "idempotent_replay": True,
                "task_id": cached.get("task_id") or "",
            })
        task_id = cached.get("task_id") or ""
        if task_id:
            return JSONResponse(
                status_code=202,
                content=IngestTaskAccepted(
                    task_id=task_id,
                    status="queued",
                    file_name=display_name,
                    doc_id=doc_id,
                    visibility=visibility,
                    message="idempotent_replay",
                ).model_dump(),
            )
        return IngestResponse(**cached)
    if begin.status == IdempotencyStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Ingest already in progress for this idempotency key")
    if begin.status == IdempotencyStatus.CONFLICT:
        raise HTTPException(status_code=409, detail="Idempotency-Key reused with a different request payload")

    acl = DocumentACL(
        doc_id=doc_id,
        tenant_id=user.tenant_id,
        owner_id=user.user_id,
        visibility=visibility,
        allowed_roles=["admin", "member", "viewer"],
        source_path=save_path,
    )
    auth_store.upsert_document_acl(acl)
    auth_store.append_audit(
        "ingest.upload",
        actor=user,
        success=True,
        resource_type="document",
        resource_id=doc_id,
        detail={
            "file_name": display_name,
            "visibility": visibility,
            "async": settings.ingest_async,
        },
        ip=(request.client.host if request.client else ""),
    )

    # ── 异步路径：落盘后入队，立即返回 202 ─────────────────
    if settings.ingest_async:
        if ingest_job_store is None or ingest_queue is None:
            state_store.fail_idempotent(key, "ingest queue unavailable")
            raise HTTPException(status_code=503, detail="Ingest queue not initialized")

        job_id = new_job_id()
        job = IngestJob(
            job_id=job_id,
            status="queued",
            file_name=display_name,
            file_path=save_path,
            doc_id=doc_id,
            visibility=visibility,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            idempotency_key=key,
            content_hash=content_hash,
        )
        ingest_job_store.create(job)
        # 幂等缓存先记下 task_id，便于客户端重试时拿到同一任务
        state_store.complete_idempotent(
            key,
            {
                "task_id": job_id,
                "status": "queued",
                "file_name": display_name,
                "doc_id": doc_id,
                "visibility": visibility,
            },
        )
        try:
            await ingest_queue.enqueue(job_id)
        except Exception as exc:
            logger.exception("enqueue ingest job failed")
            ingest_job_store.mark_failed(job_id, str(exc))
            state_store.fail_idempotent(key, str(exc))
            raise HTTPException(status_code=503, detail=f"Failed to enqueue ingest job: {exc}") from exc

        return JSONResponse(
            status_code=202,
            content=IngestTaskAccepted(
                task_id=job_id,
                status="queued",
                file_name=display_name,
                doc_id=doc_id,
                visibility=visibility,
            ).model_dump(),
        )

    # ── 同步路径（INGEST_ASYNC=false）──────────────────────
    index_rec = state_store.begin_document_index(
        doc_id,
        tenant_id=user.tenant_id,
        source_path=save_path,
        content_hash=content_hash,
    )
    try:
        result = await ingest_wf.ainvoke({
            "file_paths": [save_path],
            "acl_metadata": acl.to_chunk_metadata(),
            "doc_version": index_rec.version,
        })
        chunks = result.get("chunks", [])
        extractions = result.get("extractions", [])
        total_entities = sum(len(e.entities) for e in extractions)
        total_relations = sum(len(e.relations) for e in extractions)

        parse_failed = (
            not chunks
            or all(
                (c.content or "").startswith("[PDF 解析失败]")
                or (c.content or "").startswith("[解析失败]")
                for c in chunks
            )
        )
        if parse_failed:
            detail = (chunks[0].content if chunks else "empty parse result")[:500]
            state_store.finalize_document_index(
                doc_id, index_rec.version, DocumentIndexStatus.FAILED, detail
            )
            state_store.fail_idempotent(key, detail)
            raise HTTPException(
                status_code=422,
                detail=f"Document parse failed: {detail}. Check server logs and PDF deps (PyPDF2/pdf2image/poppler).",
            )

        from services.ingest_storage import assess_ingest_storage

        store_ok, store_err = assess_ingest_storage(result)
        if not store_ok:
            state_store.finalize_document_index(
                doc_id, index_rec.version, DocumentIndexStatus.FAILED, store_err
            )
            state_store.fail_idempotent(key, store_err)
            raise HTTPException(
                status_code=503,
                detail=f"Ingest storage failed (document not ready): {store_err}",
            )

        response = IngestResponse(
            file_name=display_name,
            chunks_count=len(chunks),
            entities_count=total_entities,
            relations_count=total_relations,
            status="success",
            doc_id=doc_id,
            visibility=visibility,
            idempotent_replay=False,
        )
        state_store.set_file_hash(save_path, content_hash)
        state_store.finalize_document_index(
            doc_id, index_rec.version, DocumentIndexStatus.READY
        )
        state_store.complete_idempotent(key, response.model_dump())
        return response
    except HTTPException:
        raise
    except Exception as e:
        state_store.finalize_document_index(
            doc_id, index_rec.version, DocumentIndexStatus.FAILED, str(e)
        )
        state_store.fail_idempotent(key, str(e))
        raise


@app.get("/api/ingest/tasks/{task_id}", response_model=IngestTaskStatus, tags=["文档入库"])
async def get_ingest_task(task_id: str, user: User = Depends(get_current_user)):
    """查询异步入库任务状态。"""
    if ingest_job_store is None:
        raise HTTPException(status_code=503, detail="Ingest job store not initialized")
    job = ingest_job_store.get(task_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if user.role == "admin":
        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Task not found")
    elif job.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return IngestTaskStatus(
        task_id=job.job_id,
        status=job.status,
        file_name=job.file_name,
        doc_id=job.doc_id,
        visibility=job.visibility,
        error=job.error,
        result=job.result,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@app.get("/api/ingest/tasks", response_model=list[IngestTaskStatus], tags=["文档入库"])
async def list_ingest_tasks(
    limit: int = 50,
    user: User = Depends(get_current_user),
):
    """列出当前用户的入库任务（admin 可见本租户）。"""
    if ingest_job_store is None:
        raise HTTPException(status_code=503, detail="Ingest job store not initialized")
    limit = max(1, min(limit, 200))
    if user.role == "admin":
        jobs = ingest_job_store.list_jobs(tenant_id=user.tenant_id, limit=limit)
    else:
        jobs = ingest_job_store.list_jobs(user_id=user.user_id, limit=limit)
    return [
        IngestTaskStatus(
            task_id=j.job_id,
            status=j.status,
            file_name=j.file_name,
            doc_id=j.doc_id,
            visibility=j.visibility,
            error=j.error,
            result=j.result,
            created_at=j.created_at,
            updated_at=j.updated_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
        )
        for j in jobs
    ]


@app.post("/api/ingest/batch", tags=["文档入库"])
async def upload_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    visibility: str = Form("tenant"),
    user: User = Depends(require_writer),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """批量上传文档（每个文件各自幂等；可选前缀 Idempotency-Key）"""
    results = []
    for idx, file in enumerate(files):
        per_key = None
        if idempotency_key:
            per_key = f"{idempotency_key}:{idx}:{file.filename or idx}"
        resp = await upload_document(
            request=request,
            file=file,
            visibility=visibility,
            user=user,
            idempotency_key=per_key,
        )
        if isinstance(resp, JSONResponse):
            import json as _json

            results.append(_json.loads(resp.body))
        elif hasattr(resp, "model_dump"):
            results.append(resp.model_dump())
        else:
            results.append(resp)
    return results


# ── QA Endpoints ─────────────────────────────────────────────

@app.post("/api/qa/ask", response_model=QuestionResponse, tags=["智能问答"])
async def ask_question(req: QuestionRequest, user: User = Depends(get_current_user)):
    """智能问答 — 混合检索 + 知识图谱推理（按当前用户 ACL 过滤，支持多轮 session）"""
    import uuid

    try:
        ensure_llm_ready()
    except LlmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rate_key = f"{user.tenant_id}:{user.user_id}"
    if not _qa_rate_limiter.allow(rate_key):
        raise HTTPException(
            status_code=429,
            detail=f"QA rate limit exceeded ({settings.rate_limit_qa_per_minute}/min)",
            headers={"Retry-After": "60"},
        )
    tenant_key = f"tenant:{user.tenant_id}"
    if not _qa_tenant_rate_limiter.allow(tenant_key):
        raise HTTPException(
            status_code=429,
            detail=(
                f"QA tenant rate limit exceeded "
                f"({settings.rate_limit_qa_per_tenant_per_minute}/min)"
            ),
            headers={"Retry-After": "60"},
        )

    qa_wf = workflows.get("qa")
    if not qa_wf:
        raise HTTPException(status_code=503, detail="QA workflow not initialized")

    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    # 会话与用户绑定，避免跨用户串话
    thread_id = f"{user.user_id}:{session_id}"

    try:
        result = await qa_wf.ainvoke(
            {
                "question": req.question,
                "history": req.history or [],
                "access_user": user,
                "session_id": session_id,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
    except Exception as exc:
        # 把 LLM / 依赖错误转成可读信息，避免前端只看到笼统 HTTP 500
        detail = str(exc)
        status = 502
        lower = detail.lower()
        if "blocked" in lower or "permission" in lower or "401" in lower or "403" in lower:
            detail = (
                "大模型接口拒绝请求（PermissionDenied/Blocked）。"
                "请检查 .env 中 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 是否有效、是否有额度。"
                f" 原始错误: {exc}"
            )
            status = 502
        elif "timeout" in lower or "connect" in lower:
            detail = f"调用大模型或下游服务超时/连接失败: {exc}"
            status = 504
        raise HTTPException(status_code=status, detail=detail) from exc

    qa_result = result.get("result")
    if not qa_result:
        raise HTTPException(status_code=500, detail="QA failed")

    return QuestionResponse(
        question=qa_result.question,
        answer=qa_result.answer,
        confidence=qa_result.confidence,
        intent=qa_result.intent.value,
        sources=[
            {"content": c.content[:200], "source": c.source, "score": c.score, "type": c.retrieval_type}
            for c in qa_result.contexts
        ],
        reasoning_steps=qa_result.reasoning_steps,
        session_id=session_id,
        resolved_question=getattr(qa_result, "resolved_question", "") or "",
        grounded=bool(getattr(qa_result, "grounded", True)),
        grounding_notes=list(getattr(qa_result, "grounding_notes", []) or []),
    )


# ── Admin Endpoints ──────────────────────────────────────────

@app.get("/api/admin/stats", response_model=StatsResponse, tags=["系统管理"])
async def get_stats(user: User = Depends(require_admin)):
    """获取系统统计信息（仅 admin）"""
    try:
        vs_stats = await vector_store.get_stats()
    except Exception:
        logger.exception("admin stats: vector_store failed")
        vs_stats = {"backend": "chroma", "total_vectors": 0, "error": "unavailable"}
    try:
        kg_stats = await knowledge_graph.get_stats()
    except Exception:
        logger.exception("admin stats: knowledge_graph failed")
        kg_stats = {"total_entities": 0, "total_relations": 0, "error": "unavailable"}
    try:
        ss_stats = state_store.get_stats()
    except Exception:
        logger.exception("admin stats: state_store failed")
        ss_stats = {"backend": "unknown", "error": "unavailable"}
    return StatsResponse(
        vector_store=vs_stats,
        knowledge_graph=kg_stats,
        state_store=ss_stats,
    )


@app.post("/api/admin/update", response_model=UpdateResponse, tags=["系统管理"])
async def trigger_update(req: UpdateRequest, user: User = Depends(require_admin)):
    """手动触发知识更新（仅 admin）"""
    update_wf = workflows.get("update")
    if not update_wf:
        raise HTTPException(status_code=503, detail="Update workflow not initialized")

    change = DocumentChange(
        file_path=DocParserAgent.normalize_path(req.file_path),
        change_type=ChangeType(req.change_type),
    )
    result = await update_wf.ainvoke({"changes": [change]})
    results = result.get("results", [])
    if not results:
        raise HTTPException(status_code=500, detail="Update failed")

    r = results[0]
    return UpdateResponse(
        file_path=r.change.file_path,
        vectors_added=r.vectors_added,
        vectors_deleted=r.vectors_deleted,
        entities_added=r.entities_added,
        relations_added=r.relations_added,
        success=r.success,
        processing_time_ms=r.processing_time_ms,
    )


@app.get("/metrics", tags=["系统管理"])
async def prometheus_metrics():
    """Prometheus scrape endpoint."""
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)


@app.get("/api/health", tags=["系统管理"])
async def health():
    emb_probe = vector_store.embeddings_probe
    emb_ok = vector_store.embeddings_available and emb_probe.get("status") != "unavailable"
    deps = {
        "vector_store": _startup_status.get("vector_store"),
        "knowledge_graph": _startup_status.get("knowledge_graph"),
        "state_store": _startup_status.get("state_store"),
        "embeddings": "ok" if emb_ok else "unavailable",
        "embeddings_probe": emb_probe,
    }
    # 运行时再探测一次关键依赖
    try:
        if vector_store._store is not None:
            await vector_store.get_stats()
            deps["vector_store_live"] = "ok"
            set_dependency_up("vector_store", True)
        else:
            deps["vector_store_live"] = "down"
            set_dependency_up("vector_store", False)
    except Exception as exc:
        deps["vector_store_live"] = f"error:{type(exc).__name__}"
        set_dependency_up("vector_store", False)
        logger.warning("health vector_store probe failed: %s", exc)

    try:
        if knowledge_graph.is_connected:
            await knowledge_graph.get_stats()
            deps["knowledge_graph_live"] = "ok"
            set_dependency_up("knowledge_graph", True)
        else:
            deps["knowledge_graph_live"] = "down"
            set_dependency_up("knowledge_graph", False)
    except Exception as exc:
        deps["knowledge_graph_live"] = f"error:{type(exc).__name__}"
        set_dependency_up("knowledge_graph", False)
        logger.warning("health knowledge_graph probe failed: %s", exc)

    try:
        state_store.get_stats()
        deps["state_store_live"] = "ok"
        set_dependency_up("state_store", True)
    except Exception as exc:
        deps["state_store_live"] = f"error:{type(exc).__name__}"
        set_dependency_up("state_store", False)
        logger.warning("health state_store probe failed: %s", exc)

    ingest_queue_stats: dict[str, Any] = {"backend": _startup_status.get("ingest_queue")}
    if ingest_queue is not None:
        try:
            ingest_queue_stats = await ingest_queue.astats()
        except Exception as exc:
            ingest_queue_stats = {"error": f"{type(exc).__name__}: {exc}"}
            logger.warning("health ingest_queue stats failed: %s", exc)
    deps["ingest_queue"] = ingest_queue_stats
    worker_visible = bool(ingest_queue_stats.get("worker_visible"))
    # arq 模式下 worker 不可见 → 降级（禁止长期 queued 无告警）
    queue_backend = str(ingest_queue_stats.get("backend") or _startup_status.get("ingest_queue") or "")
    if queue_backend == "arq" and not worker_visible:
        deps["ingest_worker"] = "down"
    elif queue_backend == "local":
        deps["ingest_worker"] = "ok" if worker_visible else "down"
    else:
        deps["ingest_worker"] = "ok" if worker_visible or queue_backend in {"local_fallback"} else "unknown"

    core_ok = (
        deps.get("vector_store_live") == "ok"
        and deps.get("state_store_live") == "ok"
        and emb_ok
    )
    # worker 挂了时不得宣称 ok（本地队列 worker 死、或 arq 无心跳）
    if deps.get("ingest_worker") == "down":
        core_ok = False

    payload = {
        "status": "ok" if core_ok else "degraded",
        "service": "com_agent_chat",
        "auth_enabled": settings.auth_enabled,
        "state_store": getattr(state_store, "backend", "unknown"),
        "startup": dict(_startup_status),
        "dependencies": deps,
        "ingest_queue": ingest_queue_stats,
        "ingest_async": settings.ingest_async,
        "embeddings_available": emb_ok,
        "vector_store_ready": vector_store._store is not None,
        "embedding_backend": settings.embedding_backend,
    }
    return JSONResponse(content=payload, status_code=200 if core_ok else 503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host=settings.api_host, port=settings.api_port, reload=True)
