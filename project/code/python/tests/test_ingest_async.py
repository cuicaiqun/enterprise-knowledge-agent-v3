"""P1-5: 异步入库任务队列与状态查询。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from api.auth.store import AuthStore
from config import settings
from services.ingest_jobs import (
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    IngestJob,
    MemoryIngestJobStore,
    new_job_id,
)
from services.ingest_queue import LocalIngestQueue
from services.ingest_runner import bind_ingest_runtime, process_ingest_job
from services.state_store import MemoryStateStore


def test_memory_job_store_lifecycle():
    store = MemoryIngestJobStore()
    store.init()
    jid = new_job_id()
    job = IngestJob(
        job_id=jid,
        status=STATUS_QUEUED,
        file_name="a.txt",
        file_path="/tmp/a.txt",
        doc_id="d1",
        visibility="tenant",
        user_id="u1",
        tenant_id="t1",
    )
    store.create(job)
    assert store.get(jid).status == STATUS_QUEUED
    store.mark_running(jid)
    assert store.get(jid).status == "running"
    store.mark_succeeded(jid, {"chunks_count": 3, "status": "success"})
    done = store.get(jid)
    assert done.status == STATUS_SUCCEEDED
    assert done.result["chunks_count"] == 3


def test_local_queue_runs_job():
    async def _run():
        seen: list[str] = []

        async def run(job_id: str):
            seen.append(job_id)
            return {"ok": True}

        q = LocalIngestQueue(run, concurrency=1)
        await q.start()
        try:
            await q.enqueue("job-1")
            for _ in range(50):
                if seen:
                    break
                await asyncio.sleep(0.02)
            assert seen == ["job-1"]
        finally:
            await q.stop()

    asyncio.run(_run())


def test_process_ingest_job_success():
    async def _run():
        job_store = MemoryIngestJobStore()
        job_store.init()
        state = MemoryStateStore()
        state.init()
        jid = new_job_id()
        job_store.create(
            IngestJob(
                job_id=jid,
                status=STATUS_QUEUED,
                file_name="x.txt",
                file_path="/tmp/x.txt",
                doc_id="doc-x",
                visibility="tenant",
                user_id="u",
                tenant_id="t",
                idempotency_key="k1",
                content_hash="abc",
            )
        )

        class _Chunk:
            content = "hello world knowledge"

        class _Ext:
            entities = [1, 2]
            relations = [1]

        wf = AsyncMock()
        wf.ainvoke = AsyncMock(
            return_value={
                "chunks": [_Chunk()],
                "extractions": [_Ext()],
                "vectors_ok": True,
                "vectors_stored": 1,
                "graph_required": False,
                "store_ok": True,
            }
        )
        bind_ingest_runtime(job_store=job_store, state_store=state, workflows={"ingest": wf})

        result = await process_ingest_job(jid)
        assert result["status"] == "success"
        assert result["chunks_count"] == 1
        assert result["index_status"] == "ready"
        assert job_store.get(jid).status == STATUS_SUCCEEDED
        assert state.is_document_ready("doc-x")

    asyncio.run(_run())


def _api_client(tmp_path: Path, monkeypatch):
    db = tmp_path / "auth.db"
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_db_path", str(db))
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    monkeypatch.setattr(settings, "auth_bootstrap_admin_username", "admin")
    monkeypatch.setattr(settings, "auth_bootstrap_admin_password", "admin123")
    monkeypatch.setattr(settings, "update_mode", "off")
    monkeypatch.setattr(settings, "ingest_async", True)
    monkeypatch.setattr(settings, "ingest_queue", "local")
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "state_store_dsn", "")
    monkeypatch.setattr(settings, "require_openai_api_key", False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")
    monkeypatch.setattr(settings, "qa_checkpoint_backend", "memory")

    from api.auth import deps as deps_mod
    from api.auth import router as router_mod
    from api.auth import store as store_mod
    from api import main as main_mod

    store = AuthStore(str(db))
    store.init()
    store_mod.auth_store = store
    main_mod.auth_store = store
    router_mod.auth_store = store
    deps_mod.auth_store = store

    async def _noop_init():
        return None

    async def _noop_close():
        return None

    def _fake_refresh():
        main_mod.vector_store._embeddings = _FakeEmb()
        main_mod.vector_store._embeddings_probe = {"status": "ok", "backend": "test"}
        return main_mod.vector_store._embeddings_probe

    class _FakeEmb:
        def embed_query(self, text):
            return [0.1] * 8

        async def aembed_query(self, text):
            return [0.1] * 8

        async def aembed_documents(self, texts):
            return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(main_mod.vector_store, "init", _noop_init)
    monkeypatch.setattr(main_mod.vector_store, "refresh_embeddings_probe", _fake_refresh)
    monkeypatch.setattr(main_mod.knowledge_graph, "init", _noop_init)
    monkeypatch.setattr(main_mod.knowledge_graph, "close", _noop_close)
    _fake_refresh()

    return TestClient(main_mod.app), main_mod


def test_upload_returns_202_and_task_status(tmp_path, monkeypatch):
    client, main_mod = _api_client(tmp_path, monkeypatch)

    class _Chunk:
        content = "async ingest content for tests"

    class _Ext:
        entities = []
        relations = []

    fake_wf = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value={
                "chunks": [_Chunk()],
                "extractions": [_Ext()],
                "vectors_ok": True,
                "vectors_stored": 1,
                "graph_required": False,
                "store_ok": True,
            }
        )
    )

    with client:
        # lifespan 已启动；替换 ingest workflow，避免真实 LLM
        main_mod.workflows["ingest"] = fake_wf
        bind_ingest_runtime(
            job_store=main_mod.ingest_job_store,
            state_store=main_mod.state_store,
            workflows=main_mod.workflows,
        )

        login = client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "admin123"},
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        files = {"file": ("hello.txt", b"hello async ingest", "text/plain")}
        r = client.post("/api/ingest/upload", files=files, headers=headers)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued"
        task_id = body["task_id"]
        assert task_id

        # 等待本地 worker 跑完
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            tr = client.get(f"/api/ingest/tasks/{task_id}", headers=headers)
            assert tr.status_code == 200
            status = tr.json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)

        assert status is not None
        assert status["status"] == "succeeded", status
        assert status["result"]["chunks_count"] == 1

        listed = client.get("/api/ingest/tasks", headers=headers)
        assert listed.status_code == 200
        assert any(t["task_id"] == task_id for t in listed.json())
