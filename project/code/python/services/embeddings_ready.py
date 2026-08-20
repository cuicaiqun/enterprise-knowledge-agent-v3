"""Embeddings 就绪检查：chat 网关 ≠ embeddings，启动/入库前必须可验。

可选路径（三选一）：
  A) OPENAI_BASE_URL 提供可用 /embeddings（EMBEDDING_BACKEND=openai）
  B) EMBEDDING_BACKEND=local（text2vec 子进程）
  C) EMBEDDING_BACKEND=chroma（Chroma ONNX MiniLM，离线）

auto：先按 URL 偏好尝试 openai/local，探测失败再降级 chroma。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EMBEDDING_SETUP_HINT = (
    "Embeddings unavailable. Choose one: "
    "(A) OPENAI_BASE_URL with working /embeddings + EMBEDDING_BACKEND=openai; "
    "(B) EMBEDDING_BACKEND=local (text2vec); "
    "(C) EMBEDDING_BACKEND=chroma (ONNX MiniLM). "
    "Chat-only gateways often return 404 for /embeddings — do not assume chat key implies embeddings."
)


class EmbeddingsNotReadyError(RuntimeError):
    """Embeddings 不可用或探测失败。"""


def probe_embedding_client(embeddings: Any, *, sample: str = "ping") -> dict[str, Any]:
    """对已构造的 embedding 客户端做一次最小探测。成功返回 status=ok。"""
    if embeddings is None:
        return {
            "status": "unavailable",
            "error": "embeddings client is None",
            "hint": EMBEDDING_SETUP_HINT,
        }
    try:
        if hasattr(embeddings, "embed_query"):
            vector = embeddings.embed_query(sample)
        elif hasattr(embeddings, "embed_documents"):
            rows = embeddings.embed_documents([sample])
            vector = rows[0] if rows else None
        else:
            return {
                "status": "unavailable",
                "error": "embeddings client has no embed_query/embed_documents",
                "hint": EMBEDDING_SETUP_HINT,
            }
        if not vector or len(vector) < 8:
            return {
                "status": "unavailable",
                "error": "embedding probe returned empty/short vector",
                "hint": EMBEDDING_SETUP_HINT,
            }
        return {"status": "ok", "dims": len(vector)}
    except Exception as exc:
        logger.warning("embedding probe failed: %s: %s", type(exc).__name__, exc)
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": EMBEDDING_SETUP_HINT,
        }


async def aprobe_embedding_client(embeddings: Any, *, sample: str = "ping") -> dict[str, Any]:
    """异步探测（优先 aembed_*，否则线程外跑同步探测）。"""
    import asyncio

    if embeddings is None:
        return probe_embedding_client(None, sample=sample)
    try:
        if hasattr(embeddings, "aembed_query"):
            vector = await embeddings.aembed_query(sample)
            if not vector or len(vector) < 8:
                return {
                    "status": "unavailable",
                    "error": "embedding probe returned empty/short vector",
                    "hint": EMBEDDING_SETUP_HINT,
                }
            return {"status": "ok", "dims": len(vector)}
        if hasattr(embeddings, "aembed_documents"):
            rows = await embeddings.aembed_documents([sample])
            vector = rows[0] if rows else None
            if not vector or len(vector) < 8:
                return {
                    "status": "unavailable",
                    "error": "embedding probe returned empty/short vector",
                    "hint": EMBEDDING_SETUP_HINT,
                }
            return {"status": "ok", "dims": len(vector)}
    except Exception as exc:
        logger.warning("async embedding probe failed: %s: %s", type(exc).__name__, exc)
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": EMBEDDING_SETUP_HINT,
        }
    return await asyncio.to_thread(probe_embedding_client, embeddings, sample=sample)


def ensure_embeddings_ready(vector_store: Any) -> None:
    """入库入口：无可用 embeddings 时快速失败，避免上传后必然 failed。"""
    if getattr(vector_store, "embeddings_available", False):
        probe = getattr(vector_store, "embeddings_probe", None) or {}
        # Real probe failed earlier but client was cleared → treat as not ready
        if probe.get("status") == "unavailable" and getattr(vector_store, "_embeddings", None) is None:
            detail = probe.get("error") or "probe failed"
            raise EmbeddingsNotReadyError(f"{detail}. {EMBEDDING_SETUP_HINT}")
        return

    raise EmbeddingsNotReadyError(EMBEDDING_SETUP_HINT)
