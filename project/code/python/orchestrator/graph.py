"""
LangGraph 编排引擎 — 4 Agent 混合编排

编排模式:
  1. 文档入库流程: DocParser → KnowledgeExtract → (VectorStore + KnowledgeGraph)
  2. 问答流程: Query → QA Agent → (VectorRetrieval ∥ GraphRetrieval) → Answer
  3. 增量更新流程: CDC Event → UpdateAgent → (Diff → Parse → Store)

使用 LangGraph StateGraph 实现有向图编排，支持条件路由和并行分支
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from agents.doc_parser_agent import DocParserAgent, DocumentChunk
from agents.knowledge_extract_agent import ExtractionResult, KnowledgeExtractAgent
from agents.knowledge_update_agent import (
    ChangeType,
    DocumentChange,
    KnowledgeUpdateAgent,
    UpdateResult,
)
from agents.qa_agent import QAAgent, QAResult
from observability.metrics import record_pipeline
from services.knowledge_graph import KnowledgeGraphService
from services.qa_checkpoint import get_qa_checkpointer
from services.vector_store import VectorStoreService

logger = logging.getLogger(__name__)


class WorkflowType(str, Enum):
    INGEST = "ingest"
    QA = "qa"
    UPDATE = "update"


# ── State Schemas ────────────────────────────────────────────

class IngestState(dict):
    """文档入库流程状态"""
    file_paths: list[str]
    chunks: list[DocumentChunk]
    extractions: list[ExtractionResult]
    vectors_stored: int
    entities_stored: int
    messages: Annotated[list, add_messages]


class QAGraphState(TypedDict, total=False):
    """问答流程状态（带 checkpointer + messages 累加）"""
    question: str
    history: list
    access_user: NotRequired[Any]
    session_id: str
    result: NotRequired[Any]
    messages: Annotated[list[BaseMessage], add_messages]


class QAState(dict):
    """兼容旧引用的问答状态别名"""
    question: str
    history: list
    access_user: Any
    session_id: str
    result: QAResult | None
    messages: Annotated[list, add_messages]


class UpdateState(dict):
    """增量更新流程状态"""
    changes: list[DocumentChange]
    results: list[UpdateResult]
    messages: Annotated[list, add_messages]


# ── Workflow Builder ─────────────────────────────────────────

def build_knowledge_graph_workflow(
    vector_store: VectorStoreService | None = None,
    knowledge_graph: KnowledgeGraphService | None = None,
    update_agent: KnowledgeUpdateAgent | None = None,
    pipelines: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """
    构建编排流水线，默认返回 {"ingest", "qa", "update"}。
    ingest worker 可传 pipelines=("ingest","update")，避免编译 QA（无需 checkpointer）。
    """
    wanted = set(pipelines or ("ingest", "qa", "update"))
    doc_parser = DocParserAgent()
    extractor = KnowledgeExtractAgent()
    if update_agent is None and "update" in wanted:
        update_agent = KnowledgeUpdateAgent(
            doc_parser=doc_parser,
            knowledge_extractor=extractor,
            vector_store=vector_store,
            knowledge_graph=knowledge_graph,
        )

    out: dict[str, Any] = {}
    if "ingest" in wanted:
        out["ingest"] = _build_ingest_graph(
            doc_parser, extractor, vector_store, knowledge_graph
        )
    if "qa" in wanted:
        qa_agent = QAAgent(vector_store=vector_store, knowledge_graph=knowledge_graph)
        out["qa"] = _build_qa_graph(qa_agent)
    if "update" in wanted:
        out["update"] = _build_update_graph(update_agent)
    return out


# ── Ingest Pipeline ─────────────────────────────────────────

def _build_ingest_graph(
    doc_parser: DocParserAgent,
    extractor: KnowledgeExtractAgent,
    vector_store: VectorStoreService | None,
    knowledge_graph: KnowledgeGraphService | None,
) -> StateGraph:

    async def parse_documents(state: dict) -> dict:
        file_paths = state.get("file_paths", [])
        chunks = await doc_parser.parse_batch(file_paths)
        acl_meta = state.get("acl_metadata") or {}
        doc_version = state.get("doc_version")
        if acl_meta or doc_version is not None:
            for chunk in chunks:
                meta = {**chunk.metadata, **acl_meta}
                if doc_version is not None:
                    meta["doc_version"] = doc_version
                    meta.setdefault("index_status", "pending")
                chunk.metadata = meta
        return {**state, "chunks": chunks}

    async def extract_knowledge(state: dict) -> dict:
        chunks = state.get("chunks", [])
        extractions = await extractor.extract(chunks)
        return {**state, "extractions": extractions}

    async def store_vectors(state: dict) -> dict:
        chunks = state.get("chunks", [])
        count = 0
        vectors_ok = True
        vectors_skipped = False
        vectors_error = ""
        if vector_store and chunks:
            try:
                if vector_store.embeddings_available:
                    count = await vector_store.add_chunks(chunks)
                    vectors_ok = count > 0
                    if not vectors_ok:
                        vectors_error = "add_chunks returned 0"
                    record_pipeline(
                        "ingest",
                        "store_vectors",
                        "ok" if vectors_ok else "error",
                    )
                else:
                    vectors_ok = False
                    vectors_skipped = True
                    vectors_error = "embeddings unavailable"
                    logger.warning("store_vectors skipped: embeddings unavailable")
                    record_pipeline("ingest", "store_vectors", "skipped")
            except Exception as exc:
                vectors_ok = False
                vectors_error = str(exc)
                logger.exception("store_vectors failed")
                record_pipeline("ingest", "store_vectors", "error")
        elif chunks and not vector_store:
            vectors_ok = False
            vectors_error = "vector store not configured"
        return {
            **state,
            "vectors_stored": count,
            "vectors_ok": vectors_ok,
            "vectors_skipped": vectors_skipped,
            "vectors_error": vectors_error,
        }

    async def store_graph(state: dict) -> dict:
        from services.knowledge_graph import resolve_tenant_id

        extractions = state.get("extractions", [])
        acl = state.get("acl_metadata") or {}
        tenant_id = resolve_tenant_id(acl.get("tenant_id"))
        source = (
            acl.get("source_path") or acl.get("source") or acl.get("doc_id") or ""
        ).strip()
        entity_count = 0
        relation_count = 0
        graph_ok = True
        graph_error = ""
        graph_required = bool(
            knowledge_graph is not None
            and getattr(knowledge_graph, "is_connected", False)
            and extractions
            and any(ext.entities or ext.relations for ext in extractions)
        )

        # 向量失败则跳过图谱，避免半成功
        if state.get("vectors_ok") is False:
            return {
                **state,
                "entities_stored": 0,
                "relations_stored": 0,
                "graph_tenant_id": tenant_id,
                "graph_ok": False,
                "graph_error": "skipped because vector store failed",
                "graph_required": graph_required,
                "store_ok": False,
                "store_error": str(state.get("vectors_error") or "vector store failed"),
            }

        if knowledge_graph and extractions:
            if not getattr(knowledge_graph, "is_connected", True):
                graph_ok = False
                graph_error = "knowledge graph not connected"
                graph_required = True
                record_pipeline("ingest", "store_graph", "error")
                # 与写失败相同：回滚本批向量，避免图谱空、向量脏
                if vector_store and state.get("chunks"):
                    doc_ids = {
                        getattr(c, "doc_id", "") for c in state["chunks"] if getattr(c, "doc_id", "")
                    }
                    for doc_id in doc_ids:
                        try:
                            await vector_store.delete_by_doc_id(doc_id)
                        except Exception:
                            logger.exception("vector rollback failed for %s", doc_id)
            else:
                try:
                    for ext in extractions:
                        for ent in ext.entities:
                            await knowledge_graph.upsert_entity(
                                ent, source=source, tenant_id=tenant_id
                            )
                            entity_count += 1
                        for rel in ext.relations:
                            await knowledge_graph.add_relation(
                                rel, source=source, tenant_id=tenant_id
                            )
                            relation_count += 1
                    record_pipeline("ingest", "store_graph", "ok")
                except Exception as exc:
                    graph_ok = False
                    graph_error = str(exc)
                    logger.exception("store_graph failed")
                    record_pipeline("ingest", "store_graph", "error")
                    # 补偿：回滚本批向量，避免图谱空、向量脏
                    if vector_store and state.get("chunks"):
                        doc_ids = {
                            getattr(c, "doc_id", "") for c in state["chunks"] if getattr(c, "doc_id", "")
                        }
                        for doc_id in doc_ids:
                            try:
                                await vector_store.delete_by_doc_id(doc_id)
                            except Exception:
                                logger.exception("vector rollback failed for %s", doc_id)

        store_ok = bool(state.get("vectors_ok", True)) and (graph_ok if graph_required else True)
        store_error = ""
        if not store_ok:
            store_error = graph_error or str(state.get("vectors_error") or "storage failed")

        return {
            **state,
            "entities_stored": entity_count,
            "relations_stored": relation_count,
            "graph_tenant_id": tenant_id,
            "graph_ok": graph_ok,
            "graph_error": graph_error,
            "graph_required": graph_required,
            "store_ok": store_ok,
            "store_error": store_error,
        }

    graph = StateGraph(dict)
    graph.add_node("parse", parse_documents)
    graph.add_node("extract", extract_knowledge)
    graph.add_node("store_vectors", store_vectors)
    graph.add_node("store_graph", store_graph)

    graph.set_entry_point("parse")
    graph.add_edge("parse", "extract")
    graph.add_edge("extract", "store_vectors")
    graph.add_edge("store_vectors", "store_graph")
    graph.add_edge("store_graph", END)

    return graph.compile()


# ── QA Pipeline ──────────────────────────────────────────────

def _history_from_messages(messages: list[Any]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for msg in messages or []:
        role = str(getattr(msg, "type", "")).lower()
        content = str(getattr(msg, "content", "") or "")
        if not content.strip():
            continue
        if role in {"human", "user"}:
            turns.append({"role": "user", "content": content})
        elif role in {"ai", "assistant"}:
            turns.append({"role": "assistant", "content": content})
    return turns


def _build_qa_graph(qa_agent: QAAgent) -> StateGraph:

    async def process_question(state: QAGraphState) -> dict:
        question = state.get("question", "")
        access_user = state.get("access_user")
        session_id = state.get("session_id", "")
        # 优先用 checkpointer 中的 messages，其次用请求携带的 history
        checkpoint_history = _history_from_messages(state.get("messages") or [])
        request_history = state.get("history") or []
        history = checkpoint_history or request_history

        result = await qa_agent.answer(
            question,
            access_user=access_user,
            history=history,
            session_id=session_id,
        )
        # 仅返回本轮增量；messages 由 add_messages reducer 累加
        return {
            "result": result,
            "messages": [
                HumanMessage(content=question),
                AIMessage(content=result.answer),
            ],
        }

    graph = StateGraph(QAGraphState)
    graph.add_node("answer", process_question)
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)

    return graph.compile(checkpointer=get_qa_checkpointer())


# ── Update Pipeline ──────────────────────────────────────────

def _build_update_graph(update_agent: KnowledgeUpdateAgent) -> StateGraph:

    async def process_updates(state: dict) -> dict:
        changes = state.get("changes", [])
        results = await update_agent.process_batch(changes)
        return {**state, "results": results}

    def should_continue(state: dict) -> str:
        results = state.get("results", [])
        failed = [r for r in results if not r.success]
        if failed:
            return "retry"
        return "done"

    async def retry_failed(state: dict) -> dict:
        results = state.get("results", [])
        failed_changes = [r.change for r in results if not r.success]
        retried = await update_agent.process_batch(failed_changes)
        all_results = [r for r in results if r.success] + retried
        return {**state, "results": all_results}

    graph = StateGraph(dict)
    graph.add_node("process", process_updates)
    graph.add_node("retry", retry_failed)

    graph.set_entry_point("process")
    graph.add_conditional_edges("process", should_continue, {"retry": "retry", "done": END})
    graph.add_edge("retry", END)

    return graph.compile()
