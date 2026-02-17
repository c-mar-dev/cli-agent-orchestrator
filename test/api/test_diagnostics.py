"""Tests for diagnostics API endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_inbox_telemetry_endpoint(client):
    payload = {
        "status_checks_total": 3,
        "jsonl_tmux_disagreements": 1,
        "rates": {
            "jsonl_vs_tmux_disagreement_rate": 0.33,
            "fallback_trigger_rate": 0.0,
            "watcher_error_rate": 0.0,
        },
    }
    db_payload = {
        "idempotency_conflict_hits": 2,
        "duplicate_rows_pruned": 1,
        "dead_letter_requeue_attempted": 1,
        "dead_letter_requeue_applied": 1,
    }
    with patch("cli_agent_orchestrator.api.main.inbox_service") as mock_inbox_service, patch(
        "cli_agent_orchestrator.api.main.get_inbox_db_telemetry_snapshot"
    ) as mock_db_telemetry:
        mock_inbox_service.get_inbox_telemetry_snapshot.return_value = payload
        mock_db_telemetry.return_value = db_payload

        response = client.get("/diagnostics/inbox/telemetry")

        assert response.status_code == 200
        assert response.json() == {**payload, "db": db_payload}


def test_jsonl_gate_diagnostics_endpoint(client):
    payload = {
        "overall_pass": True,
        "gates": {
            "jsonl_vs_tmux_disagreement_rate": {
                "value": 0.0,
                "threshold": 0.01,
                "pass": True,
            }
        },
    }
    with patch("cli_agent_orchestrator.api.main.inbox_service") as mock_inbox_service:
        mock_inbox_service.evaluate_jsonl_canary_gates.return_value = payload

        response = client.get("/diagnostics/jsonl/gates")

        assert response.status_code == 200
        assert response.json() == payload


def test_jsonl_reset_diagnostics_endpoint(client):
    payload = {
        "inbox_telemetry_reset": True,
        "parser_telemetry_reset": True,
        "reset_at": "2026-02-16T00:00:00+00:00",
    }
    with patch("cli_agent_orchestrator.api.main.inbox_service") as mock_inbox_service:
        mock_inbox_service.reset_jsonl_canary_state.return_value = payload

        response = client.post("/diagnostics/jsonl/reset")

        assert response.status_code == 200
        assert response.json() == payload
