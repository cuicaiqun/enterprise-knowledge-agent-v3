#!/usr/bin/env python3
"""
P1-3：基于 /api/health 与 /metrics 的本地告警门禁（可接 cron / CI）。

Usage:
  python scripts/check_alerts.py
  API_BASE=http://127.0.0.1:8080 python scripts/check_alerts.py
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 8.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def check_health(base: str) -> list[str]:
    alerts: list[str] = []
    status, body = _get(f"{base.rstrip('/')}/api/health")
    if status == 503:
        alerts.append(f"health HTTP 503 (degraded): {body[:200]}")
    elif status != 200:
        alerts.append(f"health unexpected HTTP {status}")
        return alerts

    import json

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        alerts.append("health response is not JSON")
        return alerts

    if data.get("status") != "ok":
        alerts.append(f"health status={data.get('status')!r}")
    deps = data.get("dependencies") or {}
    # 与 api/main.py health 一致：核心面 vector + state + embeddings
    for name in ("vector_store_live", "state_store_live"):
        val = deps.get(name)
        if val not in (None, "ok"):
            alerts.append(f"core dependency {name}={val!r}")
    emb = deps.get("embeddings")
    if emb not in (None, "ok"):
        alerts.append(f"core dependency embeddings={emb!r}")
    worker = deps.get("ingest_worker")
    if worker == "down":
        alerts.append("ingest_worker down (queue will stall)")
    return alerts


def check_metrics(base: str, *, strict: bool = False) -> list[str]:
    alerts: list[str] = []
    warnings: list[str] = []
    status, body = _get(f"{base.rstrip('/')}/metrics")
    if status != 200:
        alerts.append(f"metrics HTTP {status}")
        return alerts

    core = ("vector_store", "state_store")
    optional = ("knowledge_graph",)
    for name in core + (optional if strict else ()):
        pattern = rf'dependency_up{{name="{re.escape(name)}"}}\s+([01])'
        m = re.search(pattern, body)
        if m and m.group(1) == "0":
            if name in core:
                alerts.append(f"metrics dependency_up name={name} is 0")
            else:
                warnings.append(f"metrics dependency_up name={name} is 0")
        elif not m:
            alerts.append(f"metrics missing dependency_up for {name}")

    if not strict:
        for name in optional:
            pattern = rf'dependency_up{{name="{re.escape(name)}"}}\s+([01])'
            m = re.search(pattern, body)
            if m and m.group(1) == "0":
                warnings.append(f"optional dependency_up name={name} is 0")

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    return alerts


def main() -> int:
    base = os.environ.get("API_BASE", "http://127.0.0.1:8080").rstrip("/")
    strict = os.environ.get("ALERT_STRICT", "").strip().lower() in {"1", "true", "yes"}
    alerts: list[str] = []
    alerts.extend(check_health(base))
    alerts.extend(check_metrics(base, strict=strict))

    if alerts:
        print("ALERT check FAILED:", file=sys.stderr)
        for a in alerts:
            print(f"  - {a}", file=sys.stderr)
        return 1

    print(f"P1-3 alert check OK: {base} health + metrics dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
