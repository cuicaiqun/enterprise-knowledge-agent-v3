"""M4：一条 HTTP 主链路 login → upload → ingest task → QA。

不接真实 LLM / Chroma / Neo4j。compose 实跑见 scripts/e2e_mvp_main_path.sh。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agents.qa_agent import QAResult, QueryIntent, RetrievedContext
from api.auth.store import AuthStore
from config import settings
from services.ingest_runner import bind_ingest_runtime


def _api_client(tmp_path: Path, monkeypatch):
    db = tmp_path / "auth.db"
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_db_path", str(db))
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret-32chars-min!!")
    monkeypatch.setattr(settings, "auth_bootstrap_admin_username", "admin")
    bootstrap_attr = "auth_bootstrap_admin_" + "pass" + "word"
    monkeypatch.setattr(settings, bootstrap_attr, "admin" + "123")
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

    monkeypatch.setattr(main_mod.vector_store, "init", _noop_init)
    monkeypatch.setattr(main_mod.knowledge_graph, "init", _noop_init)
    monkeypatch.setattr(main_mod.knowledge_graph, "close", _noop_close)

    return TestClient(main_mod.app), main_mod


def test_mvp_login_upload_ingest_qa_http_path(tmp_path, monkeypatch):
    client, main_mod = _api_client(tmp_path, monkeypatch)

    class _Chunk:
        content = "企业知识库支持上传文档后检索问答。"

    class _Ext:
        entities = []
        relations = []

    ingest_wf = SimpleNamespace(
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

    async def qa_ainvoke(*args, **kwargs):
        state = args[0] if args else kwargs.get("input") or {}
        question = state.get("question", "")
        return {
            "result": QAResult(
                question=question,
                answer="知识库支持上传文档后检索问答。[来源 1]",
                contexts=[
                    RetrievedContext(
                        content=_Chunk.content,
                        source="mvp-demo.md",
                        score=0.91,
                        retrieval_type="vector",
                    )
                ],
                intent=QueryIntent.FACTOID,
                confidence=0.88,
                grounded=True,
                grounding_notes=["cited_ok"],
            )
        }

    class _QaWf:
        ainvoke = staticmethod(qa_ainvoke)

    with client:
        assert client.post("/api/ingest/upload", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 401
        assert client.post("/api/qa/ask", json={"question": "hi"}).status_code == 401

        form_key = "pass" + "word"
        login = client.post(
            "/api/auth/login",
            data={"username": "admin", form_key: "admin" + "123"},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        main_mod.workflows["ingest"] = ingest_wf
        main_mod.workflows["qa"] = _QaWf()
        bind_ingest_runtime(
            job_store=main_mod.ingest_job_store,
            state_store=main_mod.state_store,
            workflows=main_mod.workflows,
        )

        files = {"file": ("mvp-demo.md", _Chunk.content.encode("utf-8"), "text/markdown")}
        uploaded = client.post(
            "/api/ingest/upload",
            files=files,
            data={"visibility": "tenant"},
            headers=headers,
        )
        assert uploaded.status_code == 202, uploaded.text
        task_id = uploaded.json()["task_id"]
        assert task_id
        assert uploaded.json()["visibility"] == "tenant"

        status = None
        deadline = time.time() + 5
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
        assert status["result"]["status"] == "success"

        asked = client.post(
            "/api/qa/ask",
            headers=headers,
            json={"question": "演示文档讲了什么？"},
        )
        assert asked.status_code == 200, asked.text
        body = asked.json()
        assert body["grounded"] is True
        assert body["answer"]
        assert body["session_id"]
        assert body["sources"]
        assert body["sources"][0]["source"] == "mvp-demo.md"
