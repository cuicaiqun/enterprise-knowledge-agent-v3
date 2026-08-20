"""P1-3 alert gate unit tests."""

from __future__ import annotations

import json
from unittest.mock import patch

from scripts import check_alerts as alerts


def test_check_health_ok():
    body = json.dumps(
        {
            "status": "ok",
            "dependencies": {
                "vector_store_live": "ok",
                "state_store_live": "ok",
                "knowledge_graph_live": "ok",
                "embeddings": "ok",
                "ingest_worker": "ok",
            },
        }
    )
    with patch.object(alerts, "_get", return_value=(200, body)):
        assert alerts.check_health("http://test") == []


def test_check_health_embeddings_down():
    body = json.dumps(
        {
            "status": "degraded",
            "dependencies": {
                "vector_store_live": "ok",
                "state_store_live": "ok",
                "embeddings": "unavailable",
            },
        }
    )
    with patch.object(alerts, "_get", return_value=(503, body)):
        out = alerts.check_health("http://test")
        assert any("503" in x or "embeddings" in x for x in out)


def test_check_health_degraded():
    body = json.dumps({"status": "degraded", "dependencies": {}})
    with patch.object(alerts, "_get", return_value=(503, body)):
        out = alerts.check_health("http://test")
        assert any("503" in x for x in out)


def test_check_metrics_dependency_down():
    metrics = (
        'dependency_up{name="vector_store"} 0\n'
        'dependency_up{name="knowledge_graph"} 1\n'
        'dependency_up{name="state_store"} 1\n'
    )
    with patch.object(alerts, "_get", return_value=(200, metrics)):
        out = alerts.check_metrics("http://test")
        assert any("vector_store" in x for x in out)


def test_check_metrics_kg_down_is_warning_not_fail():
    metrics = (
        'dependency_up{name="vector_store"} 1\n'
        'dependency_up{name="knowledge_graph"} 0\n'
        'dependency_up{name="state_store"} 1\n'
    )
    with patch.object(alerts, "_get", return_value=(200, metrics)):
        out = alerts.check_metrics("http://test", strict=False)
        assert out == []
