"""Embeddings 探针与入库门禁。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.embeddings_ready import (
    EMBEDDING_SETUP_HINT,
    EmbeddingsNotReadyError,
    ensure_embeddings_ready,
    probe_embedding_client,
)


class _OkEmbeddings:
    def embed_query(self, text: str):
        return [0.1] * 16


class _BadEmbeddings:
    def embed_query(self, text: str):
        raise RuntimeError("Embeddings API is not supported")


def test_probe_embedding_client_ok():
    out = probe_embedding_client(_OkEmbeddings())
    assert out["status"] == "ok"
    assert out["dims"] == 16


def test_probe_embedding_client_gateway_404_style():
    out = probe_embedding_client(_BadEmbeddings())
    assert out["status"] == "unavailable"
    assert "Embeddings API is not supported" in out["error"]
    assert "EMBEDDING_BACKEND" in out["hint"]


def test_ensure_embeddings_ready_rejects_missing():
    vs = SimpleNamespace(embeddings_available=False, embeddings_probe={"status": "unavailable"})
    with pytest.raises(EmbeddingsNotReadyError) as ei:
        ensure_embeddings_ready(vs)
    assert "openai" in str(ei.value).lower() or "chroma" in str(ei.value).lower()


def test_ensure_embeddings_ready_allows_injected():
    vs = SimpleNamespace(
        embeddings_available=True,
        embeddings_probe={"status": "ok", "backend": "test"},
        _embeddings=object(),
    )
    ensure_embeddings_ready(vs)


def test_create_embeddings_auto_falls_back_to_chroma(monkeypatch):
    """auto + broken openai probe → chroma ONNX client."""
    import services.vector_store as vs_mod

    monkeypatch.delenv("DISABLE_LOCAL_EMBEDDINGS", raising=False)
    monkeypatch.setattr(vs_mod.settings, "embedding_backend", "auto")
    monkeypatch.setattr(vs_mod.settings, "openai_base_url", "https://chat-only.example/v1")
    monkeypatch.setattr(vs_mod.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(vs_mod.settings, "embedding_model", "text-embedding-3-small")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def embed_query(self, text):
            raise RuntimeError("Embeddings API is not supported")

    monkeypatch.setattr(vs_mod, "OpenAIEmbeddings", _Boom)

    class _ChromaOk:
        def embed_query(self, text):
            return [0.2] * 32

        def embed_documents(self, texts):
            return [[0.2] * 32 for _ in texts]

    monkeypatch.setattr(vs_mod, "_ChromaOnnxEmbeddings", lambda: _ChromaOk())

    client, probe = vs_mod._create_embeddings()
    assert client is not None
    assert probe["status"] == "ok"
    assert probe["backend"] == "chroma"
    assert probe.get("fallback_from") == "openai"


def test_create_embeddings_openai_explicit_does_not_silent_fallback(monkeypatch):
    import services.vector_store as vs_mod

    monkeypatch.delenv("DISABLE_LOCAL_EMBEDDINGS", raising=False)
    monkeypatch.setattr(vs_mod.settings, "embedding_backend", "openai")
    monkeypatch.setattr(vs_mod.settings, "openai_base_url", "https://chat-only.example/v1")
    monkeypatch.setattr(vs_mod.settings, "openai_api_key", "sk-test")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def embed_query(self, text):
            raise RuntimeError("Embeddings API is not supported")

    monkeypatch.setattr(vs_mod, "OpenAIEmbeddings", _Boom)
    client, probe = vs_mod._create_embeddings()
    assert client is None
    assert probe["status"] == "unavailable"
    assert probe["backend"] == "openai"


def test_local_queue_stats_worker_visible():
    import asyncio

    from services.ingest_queue import LocalIngestQueue

    async def _run():
        async def noop(job_id: str):
            return {}

        q = LocalIngestQueue(noop, concurrency=2)
        await q.start()
        try:
            st = q.stats()
            assert st["backend"] == "local"
            assert st["workers_alive"] == 2
            assert st["worker_visible"] is True
            assert st["queue_depth"] == 0
        finally:
            await q.stop()

    asyncio.run(_run())


def test_hint_mentions_three_options():
    assert "openai" in EMBEDDING_SETUP_HINT
    assert "local" in EMBEDDING_SETUP_HINT
    assert "chroma" in EMBEDDING_SETUP_HINT
