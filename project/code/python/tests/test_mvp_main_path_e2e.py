"""M4 真实 API 走通（需要已启动的 compose/uvicorn + LLM）。

  RUN_MVP_E2E=1 bash scripts/e2e_mvp_main_path.sh
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.mvp_e2e]


def _enabled() -> bool:
    return os.environ.get("RUN_MVP_E2E", "").strip().lower() in {"1", "true", "yes"}


def _base() -> str:
    return os.environ.get("MVP_API_BASE", "http://127.0.0.1:8080").rstrip("/")


def _login_secret() -> tuple[str, str]:
    user = os.environ.get("ADMIN_USER") or os.environ.get("AUTH_BOOTSTRAP_ADMIN_USERNAME") or "admin"
    secret = os.environ.get("ADMIN_PASS") or ""
    if not secret:
        pytest.skip("set ADMIN_PASS for live MVP E2E")
    return user, secret


def test_live_upload_ingest_qa_path():
    if not _enabled():
        pytest.skip("set RUN_MVP_E2E=1")

    base = _base()
    user, secret = _login_secret()
    form_key = "pass" + "word"

    with httpx.Client(base_url=base, timeout=60.0) as client:
        try:
            health = client.get("/api/health")
        except httpx.RequestError as exc:
            pytest.skip(f"API unreachable at {base}: {exc}")
        if health.status_code not in {200, 503}:
            pytest.skip(f"unexpected health HTTP {health.status_code}: {health.text[:200]}")

        login = client.post("/api/auth/login", data={"username": user, form_key: secret})
        if login.status_code != 200:
            pytest.fail(f"login failed HTTP {login.status_code}: {login.text[:300]}")
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = "MVP E2E：企业知识库支持上传文档后检索问答。\n"
        files = {"file": ("mvp-e2e.md", payload.encode("utf-8"), "text/markdown")}
        uploaded = client.post(
            "/api/ingest/upload",
            headers=headers,
            files=files,
            data={"visibility": "tenant"},
        )
        if uploaded.status_code == 503:
            pytest.skip(f"ingest unavailable (LLM/deps): {uploaded.text[:300]}")
        assert uploaded.status_code == 202, uploaded.text
        task_id = uploaded.json()["task_id"]

        status = None
        deadline = time.time() + 120
        while time.time() < deadline:
            tr = client.get(f"/api/ingest/tasks/{task_id}", headers=headers)
            assert tr.status_code == 200, tr.text
            status = tr.json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(1)
        assert status is not None
        assert status["status"] == "succeeded", status

        asked = client.post(
            "/api/qa/ask",
            headers=headers,
            json={"question": "这篇演示文档讲了什么？"},
        )
        if asked.status_code == 503:
            pytest.skip(f"QA unavailable (LLM/deps): {asked.text[:300]}")
        assert asked.status_code == 200, asked.text
        body = asked.json()
        assert body.get("answer")
        assert "grounded" in body
        Path("/tmp/mvp-e2e-qa.json").write_text(asked.text, encoding="utf-8")
