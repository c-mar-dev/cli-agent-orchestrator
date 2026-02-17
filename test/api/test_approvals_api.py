"""API tests for approval queue endpoints."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from cli_agent_orchestrator.models.approval import ApprovalStatus



def _approval_payload(status: ApprovalStatus = ApprovalStatus.PENDING) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        terminal_id="abcd1234",
        provider="codex",
        status_reason_code="waiting",
        prompt_excerpt="Allow this action? (y/n)",
        source="status",
        status=status,
        acknowledged_at=None,
        resolved_at=None,
        resolution_sender_id=None,
        resolution_message=None,
        created_at=datetime(2026, 2, 17, 0, 0, 0),
        updated_at=datetime(2026, 2, 17, 0, 0, 0),
    )


def test_list_approvals_endpoint_200(api_client):
    with patch("cli_agent_orchestrator.api.main.list_approval_requests") as mock_list:
        mock_list.return_value = [_approval_payload()]

        response = api_client.get("/terminals/abcd1234/approvals")

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["status"] == "pending"


def test_list_approvals_invalid_status_400(api_client):
    response = api_client.get("/terminals/abcd1234/approvals?status=bad")
    assert response.status_code == 400
    assert "Invalid status" in response.json()["detail"]


def test_ack_approval_sends_response_and_resolves(api_client):
    with patch("cli_agent_orchestrator.api.main.get_approval_request") as mock_get, patch(
        "cli_agent_orchestrator.api.main.acknowledge_approval_request"
    ) as mock_ack, patch("cli_agent_orchestrator.api.main.resolve_approval_request") as mock_resolve, patch(
        "cli_agent_orchestrator.api.main.terminal_service.send_input"
    ) as mock_send:
        mock_get.return_value = _approval_payload(ApprovalStatus.PENDING)
        mock_ack.return_value = _approval_payload(ApprovalStatus.ACKNOWLEDGED)
        mock_resolve.return_value = _approval_payload(ApprovalStatus.RESOLVED)

        response = api_client.post(
            "/terminals/abcd1234/approvals/1/ack",
            json={"sender_id": "human-1", "response_message": "yes", "auto_send": True},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "resolved"
        assert response.json()["sent_response"] is True
        mock_send.assert_called_once_with("abcd1234", "yes")


def test_ack_approval_requires_message_when_auto_send(api_client):
    with patch("cli_agent_orchestrator.api.main.get_approval_request") as mock_get:
        mock_get.return_value = _approval_payload(ApprovalStatus.PENDING)

        response = api_client.post(
            "/terminals/abcd1234/approvals/1/ack",
            json={"sender_id": "human-1", "auto_send": True},
        )

        assert response.status_code == 400
        assert "response_message is required" in response.json()["detail"]
