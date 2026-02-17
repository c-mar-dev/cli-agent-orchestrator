"""Integration tests for handoff/assign/send_message orchestration flows."""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from test.conftest import FakeProvider


@pytest.fixture
def requests_bridge(api_client, monkeypatch):
    """Patch mcp_server requests to route through FastAPI TestClient."""

    def _path(url: str) -> str:
        assert url.startswith(server.API_BASE_URL)
        return url[len(server.API_BASE_URL) :]

    def _get(url, params=None, timeout=None, **kwargs):
        return api_client.get(_path(url), params=params, **kwargs)

    def _post(url, params=None, timeout=None, **kwargs):
        return api_client.post(_path(url), params=params, **kwargs)

    def _delete(url, params=None, timeout=None, **kwargs):
        return api_client.delete(_path(url), params=params, **kwargs)

    monkeypatch.setattr(server.requests, "get", _get)
    monkeypatch.setattr(server.requests, "post", _post)
    monkeypatch.setattr(server.requests, "delete", _delete)


@pytest.fixture
def fake_wait_factory(fake_provider_manager):
    """Build wait_until_terminal_status implementations backed by FakeProvider states."""

    def _factory(always_false: bool = False) -> Callable:
        def _wait(
            terminal_id: str,
            target_status: TerminalStatus,
            timeout: float = 30.0,
            polling_interval: float = 1.0,
            blocked_statuses=None,
        ) -> bool:
            if always_false:
                return False
            provider = fake_provider_manager.get_provider(terminal_id)
            checks = max(1, int(timeout / max(polling_interval, 0.1)))
            for _ in range(checks):
                if provider.get_status() == target_status:
                    return True
            return False

        return _wait

    return _factory


def _create_conductor(api_client) -> str:
    response = api_client.post(
        "/sessions",
        params={"provider": "q_cli", "agent_profile": "developer", "session_name": "cao-main"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_handoff_happy_path(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    fake_wait_factory,
    monkeypatch,
):
    conductor_id = _create_conductor(api_client)
    monkeypatch.setenv("CAO_TERMINAL_ID", conductor_id)
    monkeypatch.setattr(server, "wait_until_terminal_status", fake_wait_factory())
    monkeypatch.setattr(
        fake_provider_manager,
        "create_provider",
        lambda _ptype, terminal_id, session_name, window_name, agent_profile=None: fake_provider_manager._providers.setdefault(
            terminal_id,
            FakeProvider(
                terminal_id,
                session_name,
                window_name,
                status_sequence=[
                    TerminalStatus.IDLE,
                    TerminalStatus.IDLE,
                    TerminalStatus.PROCESSING,
                    TerminalStatus.COMPLETED,
                ],
            ),
        ),
    )

    result = asyncio.run(server._handoff_impl("developer", "run task", timeout=5))

    assert result.success is True
    assert result.terminal_id is not None
    assert result.output == "fake provider output"

    metadata = database.get_terminal_metadata(result.terminal_id)
    assert metadata is not None
    assert any(keys == "/exit" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_handoff_timeout(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    fake_wait_factory,
    monkeypatch,
):
    conductor_id = _create_conductor(api_client)
    monkeypatch.setenv("CAO_TERMINAL_ID", conductor_id)
    monkeypatch.setattr(server, "wait_until_terminal_status", fake_wait_factory(always_false=True))

    result = asyncio.run(server._handoff_impl("developer", "run task", timeout=1))

    assert result.success is False
    assert result.terminal_id is not None


def test_assign_creates_terminal_and_sends_message(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    monkeypatch,
):
    conductor_id = _create_conductor(api_client)
    monkeypatch.setenv("CAO_TERMINAL_ID", conductor_id)

    result = server._assign_impl("analyst", "investigate")

    assert result["success"] is True
    assert result["terminal_id"] is not None
    assert any(keys == "investigate" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_send_message_creates_inbox_record(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    monkeypatch,
):
    sender_id = _create_conductor(api_client)
    receiver_resp = api_client.post(
        "/sessions/cao-main/terminals",
        params={"provider": "q_cli", "agent_profile": "reviewer"},
    )
    assert receiver_resp.status_code == 201
    receiver_id = receiver_resp.json()["id"]

    # Keep receiver not-ready for immediate delivery.
    receiver_provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(receiver_provider, FakeProvider)
    receiver_provider.status_sequence = [TerminalStatus.PROCESSING, TerminalStatus.PROCESSING]

    monkeypatch.setenv("CAO_TERMINAL_ID", sender_id)
    result = server._send_to_inbox(receiver_id, "queued hello")

    assert result["success"] is True
    messages = database.get_inbox_messages(receiver_id, limit=10)
    assert len(messages) == 1
    assert messages[0].message == "queued hello"


def test_send_message_then_manual_delivery_when_idle(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    monkeypatch,
):
    sender_id = _create_conductor(api_client)
    receiver_resp = api_client.post(
        "/sessions/cao-main/terminals",
        params={"provider": "q_cli", "agent_profile": "reviewer"},
    )
    receiver_id = receiver_resp.json()["id"]

    receiver_provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(receiver_provider, FakeProvider)
    receiver_provider.status_sequence = [TerminalStatus.PROCESSING, TerminalStatus.IDLE]

    monkeypatch.setenv("CAO_TERMINAL_ID", sender_id)
    server._send_to_inbox(receiver_id, "deliver me")

    from cli_agent_orchestrator.services import inbox_service

    delivered = inbox_service.check_and_send_pending_messages(receiver_id)
    assert delivered is True

    messages = database.get_inbox_messages(receiver_id, limit=10)
    assert len(messages) == 1
    assert messages[0].status == MessageStatus.DELIVERED


def test_send_message_stays_pending_when_processing(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    requests_bridge,
    monkeypatch,
):
    sender_id = _create_conductor(api_client)
    receiver_resp = api_client.post(
        "/sessions/cao-main/terminals",
        params={"provider": "q_cli", "agent_profile": "reviewer"},
    )
    receiver_id = receiver_resp.json()["id"]

    receiver_provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(receiver_provider, FakeProvider)
    receiver_provider.status_sequence = [TerminalStatus.PROCESSING, TerminalStatus.PROCESSING]

    monkeypatch.setenv("CAO_TERMINAL_ID", sender_id)
    server._send_to_inbox(receiver_id, "hold")

    from cli_agent_orchestrator.services import inbox_service

    sent = inbox_service.check_and_send_pending_messages(receiver_id)
    assert sent is False

    pending = database.get_inbox_messages(receiver_id, status=MessageStatus.PENDING)
    assert len(pending) >= 1
