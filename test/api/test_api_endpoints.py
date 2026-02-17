"""Route-layer API tests for uncovered endpoints."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.models.inbox import MessageStatus


def _terminal_payload(terminal_id: str = "abcd1234") -> dict:
    return {
        "id": terminal_id,
        "name": "dev-1",
        "provider": "q_cli",
        "session_name": "cao-main",
        "agent_profile": "developer",
        "status": "idle",
        "last_active": datetime.now(),
    }


def test_health_check(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_list_sessions_200(api_client):
    with patch("cli_agent_orchestrator.api.main.session_service") as mock_service:
        mock_service.list_sessions.return_value = [{"id": "cao-main"}]

        response = api_client.get("/sessions")

        assert response.status_code == 200
        assert response.json() == [{"id": "cao-main"}]


def test_get_session_200(api_client):
    payload = {"session": {"id": "cao-main"}, "terminals": []}
    with patch("cli_agent_orchestrator.api.main.session_service") as mock_service:
        mock_service.get_session.return_value = payload

        response = api_client.get("/sessions/cao-main")

        assert response.status_code == 200
        assert response.json() == payload


def test_get_session_404(api_client):
    with patch("cli_agent_orchestrator.api.main.session_service") as mock_service:
        mock_service.get_session.side_effect = ValueError("not found")

        response = api_client.get("/sessions/cao-missing")

        assert response.status_code == 404


def test_delete_session_200(api_client):
    with patch("cli_agent_orchestrator.api.main.session_service") as mock_service:
        mock_service.delete_session.return_value = True

        response = api_client.delete("/sessions/cao-main")

        assert response.status_code == 200
        assert response.json() == {"success": True}


def test_get_terminal_200(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.get_terminal.return_value = _terminal_payload()

        response = api_client.get("/terminals/abcd1234")

        assert response.status_code == 200
        assert response.json()["id"] == "abcd1234"


def test_get_terminal_404(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.get_terminal.side_effect = ValueError("not found")

        response = api_client.get("/terminals/abcd1234")

        assert response.status_code == 404


def test_send_input_200(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.send_input.return_value = True

        response = api_client.post("/terminals/abcd1234/input", params={"message": "hello"})

        assert response.status_code == 200
        assert response.json() == {"success": True}


def test_send_input_404(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.send_input.side_effect = ValueError("not found")

        response = api_client.post("/terminals/abcd1234/input", params={"message": "hello"})

        assert response.status_code == 404


def test_get_output_full_mode_200(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.get_output.return_value = "full output"

        response = api_client.get("/terminals/abcd1234/output", params={"mode": "full"})

        assert response.status_code == 200
        assert response.json()["output"] == "full output"
        assert response.json()["mode"] == "full"


def test_get_output_last_mode_200(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.get_output.return_value = "last output"

        response = api_client.get("/terminals/abcd1234/output", params={"mode": "last"})

        assert response.status_code == 200
        assert response.json()["output"] == "last output"
        assert response.json()["mode"] == "last"


def test_exit_terminal_200_sends_provider_exit(api_client):
    mock_provider = Mock()
    mock_provider.exit_cli.return_value = "/exit"

    with patch("cli_agent_orchestrator.api.main.provider_manager") as mock_provider_manager, patch(
        "cli_agent_orchestrator.api.main.terminal_service"
    ) as mock_terminal_service:
        mock_provider_manager.get_provider.return_value = mock_provider
        mock_terminal_service.send_input.return_value = True

        response = api_client.post("/terminals/abcd1234/exit")

        assert response.status_code == 200
        assert response.json() == {"success": True}
        mock_provider_manager.get_provider.assert_called_once_with("abcd1234")
        mock_terminal_service.send_input.assert_called_once_with("abcd1234", "/exit")


def test_delete_terminal_200(api_client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_service:
        mock_service.delete_terminal.return_value = True

        response = api_client.delete("/terminals/abcd1234")

        assert response.status_code == 200
        assert response.json() == {"success": True}


def test_create_inbox_message_201_calls_create_and_delivery(api_client):
    inbox_msg = SimpleNamespace(
        id=1,
        sender_id="sender1",
        receiver_id="abcd1234",
        message="hello",
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )

    with patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create, patch(
        "cli_agent_orchestrator.api.main.inbox_service.check_and_send_pending_messages"
    ) as mock_deliver:
        mock_create.return_value = inbox_msg

        response = api_client.post(
            "/terminals/abcd1234/inbox/messages",
            params={"sender_id": "sender1", "message": "hello"},
        )

        assert response.status_code == 201
        assert response.json()["success"] is True
        mock_create.assert_called_once_with(
            "sender1",
            "abcd1234",
            "hello",
            idempotency_key=None,
            max_attempts=None,
            requeue_terminal_state=api_main.CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
        )
        mock_deliver.assert_called_once_with("abcd1234")


def test_create_inbox_message_delivery_exception_returns_500(api_client):
    inbox_msg = SimpleNamespace(
        id=1,
        sender_id="sender1",
        receiver_id="abcd1234",
        message="hello",
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )

    with patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create, patch(
        "cli_agent_orchestrator.api.main.inbox_service.check_and_send_pending_messages"
    ) as mock_deliver:
        mock_create.return_value = inbox_msg
        mock_deliver.side_effect = RuntimeError("delivery failed")

        response = api_client.post(
            "/terminals/abcd1234/inbox/messages",
            params={"sender_id": "sender1", "message": "hello"},
        )

        assert response.status_code == 500
        mock_create.assert_called_once_with(
            "sender1",
            "abcd1234",
            "hello",
            idempotency_key=None,
            max_attempts=None,
            requeue_terminal_state=api_main.CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
        )
        mock_deliver.assert_called_once_with("abcd1234")


def test_create_inbox_message_requeue_terminal_state_param_forwarded(api_client):
    inbox_msg = SimpleNamespace(
        id=1,
        sender_id="sender1",
        receiver_id="abcd1234",
        message="hello",
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )

    with patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create, patch(
        "cli_agent_orchestrator.api.main.inbox_service.check_and_send_pending_messages"
    ) as mock_deliver:
        mock_create.return_value = inbox_msg

        response = api_client.post(
            "/terminals/abcd1234/inbox/messages",
            params={
                "sender_id": "sender1",
                "message": "hello",
                "requeue_terminal_state": True,
            },
        )

        assert response.status_code == 201
        mock_create.assert_called_once_with(
            "sender1",
            "abcd1234",
            "hello",
            idempotency_key=None,
            max_attempts=None,
            requeue_terminal_state=True,
        )
        mock_deliver.assert_called_once_with("abcd1234")
