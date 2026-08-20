"""
向量存储服务 — 支持 ChromaDB / PGVector 双后端

职责:
  1. 文档块向量化 (Embedding)
  2. 向量存储 & 检索
  3. 按 doc_id 删除（支持增量更新）
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import OpenAIEmbeddings

from agents.doc_parser_agent import DocumentChunk
from config import settings

logger = logging.getLogger(__name__)


class _SubprocessEmbeddings:
    """Embedding wrapper that delegates to a separate subprocess to avoid
    PyTorch segfaults from crashing the main server process."""

    def __init__(self):
        from services.embedding_worker import get_embedding_client
        self._client = get_embedding_client()
        if self._client is None:
            raise RuntimeError("Local embedding worker failed to start")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._client.encode(texts)
        if not vectors or len(vectors[0]) < 8:
            raise RuntimeError("Local embedding worker returned invalid vectors")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vectors = self._client.encode([text])
        if not vectors or len(vectors[0]) < 8:
            raise RuntimeError("Local embedding worker returned invalid query vector")
        return vectors[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._client.aencode(texts)
        if not vectors or len(vectors[0]) < 8:
            raise RuntimeError("Local embedding worker returned invalid vectors")
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        result = await self._client.aencode([text])
        if not result or len(result[0]) < 8:
            raise RuntimeError("Local embedding worker returned invalid query vector")
        return result[0]


class _ChromaOnnxEmbeddings:
    """Chroma 自带 ONNX MiniLM，不依赖 HuggingFace / 网关 embedding 模型。"""

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._ef = DefaultEmbeddingFunction()

    @staticmethod
    def _as_float_lists(vectors) -> list[list[float]]:
        return [[float(x) for x in row] for row in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._as_float_lists(self._ef(texts))

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        import asyncio

        return await asyncio.to_thread(self.embed_query, text)


def _create_embeddings():
    """根据配置创建 Embedding 实例，并对远程后端做最小探针。

    backend:
      - openai: OpenAI 兼容 /embeddings（须真实可用）
      - local: text2vec 子进程
      - chroma: Chroma ONNX MiniLM（离线可用）
      - auto: deepseek URL → local，否则 openai；构造或探针失败再降级 chroma

    返回 (embeddings|None, probe_dict)。
    """
    import os

    from services.embeddings_ready import EMBEDDING_SETUP_HINT, probe_embedding_client

    if os.environ.get("DISABLE_LOCAL_EMBEDDINGS") == "1":
        return None, {
            "status": "skipped",
            "backend": "disabled",
            "error": "DISABLE_LOCAL_EMBEDDINGS=1",
            "hint": EMBEDDING_SETUP_HINT,
        }
    backend = (settings.embedding_backend or "auto").strip().lower()

    def _openai():
        return OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )

    def _probe(client: Any, name: str) -> tuple[Any | None, dict]:
        result = probe_embedding_client(client)
        result["backend"] = name
        if result.get("status") == "ok":
            return client, result
        return None, result

    if backend == "openai":
        try:
            client = _openai()
        except Exception as exc:
            logger.exception("OpenAI embeddings construct failed")
            return None, {
                "status": "unavailable",
                "backend": "openai",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": EMBEDDING_SETUP_HINT,
            }
        return _probe(client, "openai")

    if backend == "local":
        try:
            client = _SubprocessEmbeddings()
        except Exception as exc:
            logger.exception("Local embeddings failed")
            return None, {
                "status": "unavailable",
                "backend": "local",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": EMBEDDING_SETUP_HINT,
            }
        return _probe(client, "local")

    if backend in {"chroma", "onnx"}:
        try:
            client = _ChromaOnnxEmbeddings()
        except Exception as exc:
            logger.exception("Chroma ONNX embeddings failed")
            return None, {
                "status": "unavailable",
                "backend": "chroma",
                "error": f"{type(exc).__name__}: {exc}",
                "hint": EMBEDDING_SETUP_HINT,
            }
        return _probe(client, "chroma")

    # auto: prefer local for deepseek chat URLs; else openai; always fall back to chroma
    prefer_local = "deepseek" in (settings.openai_base_url or "").lower()
    primary_name = "local" if prefer_local else "openai"
    try:
        primary = _SubprocessEmbeddings() if prefer_local else _openai()
        client, probe = _probe(primary, primary_name)
        if client is not None:
            return client, probe
        logger.warning(
            "Primary embedding backend %s probe failed (%s); falling back to Chroma ONNX",
            primary_name,
            probe.get("error"),
        )
    except Exception as exc:
        logger.warning(
            "Primary embedding backend %s failed (%s); falling back to Chroma ONNX",
            primary_name,
            exc,
        )
        probe = {
            "status": "unavailable",
            "backend": primary_name,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": EMBEDDING_SETUP_HINT,
        }

    try:
        client = _ChromaOnnxEmbeddings()
        chroma_client, chroma_probe = _probe(client, "chroma")
        if chroma_client is not None:
            chroma_probe["fallback_from"] = primary_name
            chroma_probe["primary_error"] = probe.get("error")
            return chroma_client, chroma_probe
        return None, chroma_probe
    except Exception as exc:
        logger.exception("Chroma ONNX fallback failed")
        return None, {
            "status": "unavailable",
            "backend": "chroma",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": EMBEDDING_SETUP_HINT,
            "primary_error": probe.get("error"),
        }


class VectorStoreService:
    """向量库统一接口，底层可切换 ChromaDB / PGVector"""

    COLLECTION_NAME = "knowledge_chunks"

    def __init__(self) -> None:
        self._embeddings: Any = None
        self._embeddings_probe: dict[str, Any] = {"status": "pending"}
        self._store: Any = None
        self._backend = settings.vector_store_type
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(max_workers=2)
        # Optional: Callable[[doc_id], str] -> allow|deny|unknown (P0-2)
        self._doc_search_gate: Any = None

    def set_doc_searchable_checker(self, checker: Any) -> None:
        """Inject state_store.document_search_gate or is_document_searchable.

        Accepts either a gate returning allow|deny|unknown, or a bool checker
        (True=allow, False=deny). Bool checkers cannot express \"unknown\".
        """
        self._doc_search_gate = checker

    def _metadata_is_searchable(self, metadata: dict[str, Any] | None) -> bool:
        meta = metadata or {}
        status = str(meta.get("index_status") or "").strip().lower()
        doc_id = str(meta.get("doc_id") or "").strip()
        if self._doc_search_gate and doc_id:
            try:
                gate = self._doc_search_gate(doc_id)
            except Exception:
                logger.exception("doc search gate failed for %s", doc_id)
                return False
            if gate is True or gate == "allow":
                return True
            if gate is False or gate == "deny":
                return False
            # unknown / other → fall through to metadata
        if status in {"pending", "failed"}:
            return False
        return True

    async def _run_sync(self, fn, *args, **kwargs):
        """Run chromadb operations in thread pool to avoid async segfaults."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    @property
    def embeddings(self):
        if self._embeddings is None and self._embeddings_probe.get("status") == "pending":
            import os
            # Skip HuggingFace embedding model if it causes instability
            # Use DISABLE_LOCAL_EMBEDDINGS=1 to force LLM-only mode
            if os.environ.get("DISABLE_LOCAL_EMBEDDINGS") == "1":
                self._embeddings_probe = {
                    "status": "skipped",
                    "backend": "disabled",
                    "error": "DISABLE_LOCAL_EMBEDDINGS=1",
                }
                return None
            try:
                client, probe = _create_embeddings()
                self._embeddings = client
                self._embeddings_probe = probe
            except Exception as exc:
                self._embeddings = None
                self._embeddings_probe = {
                    "status": "unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return self._embeddings

    @property
    def embeddings_probe(self) -> dict[str, Any]:
        # Touch embeddings to populate probe lazily
        _ = self.embeddings
        return dict(self._embeddings_probe or {})

    def refresh_embeddings_probe(self) -> dict[str, Any]:
        """强制重建并探测 embeddings（启动 / health 用）。"""
        self._embeddings = None
        self._embeddings_probe = {"status": "pending"}
        _ = self.embeddings
        return self.embeddings_probe

    @property
    def embeddings_available(self) -> bool:
        # Injected/faked embeddings (tests) must remain usable even when
        # DISABLE_LOCAL_EMBEDDINGS=1 blocks creating a real model.
        if self._embeddings is not None:
            return True
        import os
        if os.environ.get("DISABLE_LOCAL_EMBEDDINGS") == "1":
            return False
        # Try loading; if it fails, stay disabled
        try:
            return self.embeddings is not None
        except Exception:
            return False

    # ── initialization ───────────────────────────────────────

    async def init(self) -> None:
        if self._backend == "chroma":
            await self._init_chroma()
        else:
            await self._init_pgvector()

    async def _init_chroma(self) -> None:
        def _init():
            import chromadb
            # Chroma runs as a separate service. Keeping its native extension
            # outside the API process avoids the async-process crash that the
            # previous PersistentClient workaround tried to avoid.
            client = chromadb.HttpClient(
                host=settings.chroma_host,
                port=settings.chroma_port,
            )
            return client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        self._store = await self._run_sync(_init)

    async def _init_pgvector(self) -> None:
        from langchain_community.vectorstores import PGVector
        self._store = PGVector(
            connection_string=settings.pgvector_dsn,
            collection_name=self.COLLECTION_NAME,
            embedding_function=self.embeddings,
        )

    # ── CRUD ─────────────────────────────────────────────────

    async def add_chunks(self, chunks: list[DocumentChunk]) -> int:
        """向量化并存储文档块，返回成功写入（或更新）的块数。"""
        if not chunks or not self.embeddings_available:
            return 0
        if self._store is None:
            raise RuntimeError("Vector store has not been initialized")

        texts = [chunk.content for chunk in chunks]
        vectors = await self.embeddings.aembed_documents(texts)

        if self._backend == "chroma":
            ids = [chunk.chunk_id for chunk in chunks]
            metadatas = [
                {
                    "doc_id": chunk.doc_id,
                    "chunk_index": chunk.chunk_index,
                    "doc_type": chunk.doc_type.value,
                    **chunk.metadata,
                }
                for chunk in chunks
            ]
            await self._run_sync(
                self._store.upsert,
                ids=ids,
                documents=texts,
                embeddings=vectors,
                metadatas=metadatas,
            )
        else:
            from langchain_core.documents import Document

            documents = [
                Document(
                    page_content=chunk.content,
                    metadata={
                        "doc_id": chunk.doc_id,
                        "chunk_index": chunk.chunk_index,
                        "doc_type": chunk.doc_type.value,
                        **chunk.metadata,
                    },
                )
                for chunk in chunks
            ]
            ids = [chunk.chunk_id for chunk in chunks]
            await self._run_sync(self._store.add_documents, documents, ids=ids)
        return len(chunks)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        where: dict | None = None,
        access_user: Any = None,
    ) -> list[tuple[dict, float]]:
        """语义搜索，统一返回“越大越相关”的分数。可选 ACL where + 后置过滤。"""
        if not self.embeddings_available:
            return []
        if self._store is None:
            raise RuntimeError("Vector store has not been initialized")
        if self._backend == "chroma":
            query_embedding = await self.embeddings.aembed_query(query)
            kwargs: dict[str, Any] = {
                "query_embeddings": [query_embedding],
                "n_results": top_k,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            try:
                result = await self._run_sync(self._store.query, **kwargs)
            except Exception:
                # 旧数据无 ACL 字段或 where 不被支持时，回退为全量检索再后置过滤
                if where:
                    result = await self._run_sync(
                        self._store.query,
                        query_embeddings=[query_embedding],
                        n_results=max(top_k * 5, top_k),
                        include=["documents", "metadatas", "distances"],
                    )
                else:
                    raise
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            pairs = [
                (
                    {
                        "content": content,
                        "source": metadata.get("source", "vector_store"),
                        "metadata": metadata,
                    },
                    max(0.0, 1.0 - float(distance)),
                )
                for content, metadata, distance in zip(documents, metadatas, distances)
            ]
            if access_user is not None:
                from api.auth.acl import can_access_document

                pairs = [
                    (doc, score)
                    for doc, score in pairs
                    if can_access_document(access_user, doc.get("metadata") or {})
                ]
            pairs = [
                (doc, score)
                for doc, score in pairs
                if self._metadata_is_searchable(doc.get("metadata") or {})
            ]
            return pairs[:top_k]
        results = await self._store.asimilarity_search_with_score(query, k=top_k)
        pairs = [
            ({"content": doc.page_content, "source": doc.metadata.get("source", ""), "metadata": doc.metadata}, score)
            for doc, score in results
        ]
        if access_user is not None:
            from api.auth.acl import can_access_document

            pairs = [
                (doc, score)
                for doc, score in pairs
                if can_access_document(access_user, doc.get("metadata") or {})
            ]
        pairs = [
            (doc, score)
            for doc, score in pairs
            if self._metadata_is_searchable(doc.get("metadata") or {})
        ]
        return pairs[:top_k]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        """按 doc_id 删除所有相关向量（Chroma + PGVector）。

        存储未连接时必须失败，禁止静默返回 0（否则删除/回滚会被当成成功）。
        """
        if self._store is None:
            raise RuntimeError("vector store not connected")
        if self._backend == "chroma":
            existing = await self._run_sync(self._store.get, where={"doc_id": doc_id}, include=[])
            ids = existing.get("ids", [])
            if ids:
                await self._run_sync(self._store.delete, ids=ids)
            return len(ids)

        # pgvector / langchain_pg_embedding
        deleted = await self._run_sync(self._delete_pgvector_by_doc_id, doc_id)
        return int(deleted)

    def _delete_pgvector_by_doc_id(self, doc_id: str) -> int:
        from sqlalchemy import create_engine, text

        from config import settings

        engine = create_engine(settings.pgvector_dsn)
        try:
            with engine.begin() as conn:
                # 优先按自定义 uuid/id（add_documents ids=chunk_id）
                result = conn.execute(
                    text(
                        """
                        DELETE FROM langchain_pg_embedding AS e
                        USING langchain_pg_collection AS c
                        WHERE e.collection_id = c.uuid
                          AND c.name = :cname
                          AND (
                            e.cmetadata->>'doc_id' = :doc_id
                            OR e.uuid::text LIKE :id_prefix
                            OR e.custom_id LIKE :id_prefix
                          )
                        """
                    ),
                    {"cname": self.COLLECTION_NAME, "doc_id": doc_id, "id_prefix": f"{doc_id}#%"},
                )
                return int(result.rowcount or 0)
        except Exception:
            # 兼容无 custom_id 列的旧 schema：仅按 cmetadata
            try:
                with engine.begin() as conn:
                    result = conn.execute(
                        text(
                            """
                            DELETE FROM langchain_pg_embedding AS e
                            USING langchain_pg_collection AS c
                            WHERE e.collection_id = c.uuid
                              AND c.name = :cname
                              AND e.cmetadata->>'doc_id' = :doc_id
                            """
                        ),
                        {"cname": self.COLLECTION_NAME, "doc_id": doc_id},
                    )
                    return int(result.rowcount or 0)
            except Exception:
                logger.exception("pgvector delete_by_doc_id failed for %s", doc_id)
                return 0
        finally:
            engine.dispose()

    async def get_stats(self) -> dict:
        """获取真实向量库统计信息。"""
        if self._backend == "chroma":
            if self._store is None:
                return {"backend": "chroma", "total_vectors": 0, "collection": self.COLLECTION_NAME}
            total_vectors = await self._run_sync(self._store.count)
            return {"backend": "chroma", "total_vectors": total_vectors, "collection": self.COLLECTION_NAME}
        return {"backend": "pgvector", "collection": self.COLLECTION_NAME}
