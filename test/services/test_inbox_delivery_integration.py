"""Integration tests for inbox delivery pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from watchdog.events import FileModifiedEvent

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, terminal_service
from test.conftest import FakeProvider


def _create_receiver(session_name: str = "cao-inbox") -> str:
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name=session_name,
        new_session=True,
    )
    return terminal.id


def test_fifo_delivery_order(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-fifo")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.IDLE, TerminalStatus.IDLE, TerminalStatus.IDLE]

    database.create_inbox_message("s1", receiver_id, "one")
    database.create_inbox_message("s2", receiver_id, "two")
    database.create_inbox_message("s3", receiver_id, "three")

    assert inbox_service.check_and_send_pending_messages(receiver_id) is True
    assert inbox_service.check_and_send_pending_messages(receiver_id) is True
    assert inbox_service.check_and_send_pending_messages(receiver_id) is True

    sent_messages = [keys for (_s, _w, keys) in fake_tmux._keys_sent]
    assert sent_messages[-3:] == ["one", "two", "three"]


def test_skips_processing_terminal(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-processing")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.PROCESSING]

    database.create_inbox_message("s1", receiver_id, "hold")

    sent = inbox_service.check_and_send_pending_messages(receiver_id)
    assert sent is False

    pending = database.get_inbox_messages(receiver_id, status=MessageStatus.PENDING)
    assert len(pending) == 1


def test_delivers_on_completed_status(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-completed")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.COMPLETED]

    database.create_inbox_message("s1", receiver_id, "after completion")

    assert inbox_service.check_and_send_pending_messages(receiver_id) is True
    delivered = database.get_inbox_messages(receiver_id, status=MessageStatus.DELIVERED)
    assert len(delivered) == 1


def test_delivers_on_waiting_user_answer(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-waiting")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.WAITING_USER_ANSWER]

    database.create_inbox_message("s1", receiver_id, "answer prompt")

    assert inbox_service.check_and_send_pending_messages(receiver_id) is True
    delivered = database.get_inbox_messages(receiver_id, status=MessageStatus.DELIVERED)
    assert len(delivered) == 1


def test_send_failure_schedules_retry(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-failed")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.IDLE]

    database.create_inbox_message("s1", receiver_id, "will fail")
    fake_tmux._raise_on_send_keys = True

    delivered = inbox_service.check_and_send_pending_messages(receiver_id)
    assert delivered is False

    retrying = database.get_inbox_messages(receiver_id, status=MessageStatus.RETRYING)
    assert len(retrying) == 1
    assert retrying[0].attempt_count == 1


def test_send_failure_dead_letters_when_max_attempts_exhausted(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_id = _create_receiver("cao-dead-letter")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.IDLE]

    database.create_inbox_message("s1", receiver_id, "will dead-letter", max_attempts=1)
    fake_tmux._raise_on_send_keys = True

    delivered = inbox_service.check_and_send_pending_messages(receiver_id)
    assert delivered is False

    dead = database.get_inbox_messages(receiver_id, status=MessageStatus.DEAD_LETTER)
    assert len(dead) == 1
    assert dead[0].attempt_count == 1


def test_poll_pending_deliveries_once_delivers_multiple_receivers(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver_a = _create_receiver("cao-poll-a")
    receiver_b = _create_receiver("cao-poll-b")

    provider_a = fake_provider_manager.get_provider(receiver_a)
    provider_b = fake_provider_manager.get_provider(receiver_b)
    assert isinstance(provider_a, FakeProvider)
    assert isinstance(provider_b, FakeProvider)
    provider_a.status_sequence = [TerminalStatus.IDLE]
    provider_b.status_sequence = [TerminalStatus.IDLE]

    database.create_inbox_message("s1", receiver_a, "msg-a")
    database.create_inbox_message("s2", receiver_b, "msg-b")

    delivered_count = inbox_service.poll_pending_deliveries_once()
    assert delivered_count == 2


def test_log_file_handler_triggers_delivery(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    receiver_id = _create_receiver("cao-log-handler")
    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.IDLE]

    database.create_inbox_message("s1", receiver_id, "from watcher")

    log_path = terminal_log_dir / f"{receiver_id}.log"
    log_path.write_text("idle prompt\n", encoding="utf-8")

    monkeypatch.setattr(inbox_service, "_has_idle_pattern", lambda _tid: True)

    handler = inbox_service.LogFileHandler()
    handler.on_modified(FileModifiedEvent(str(log_path)))

    delivered = database.get_inbox_messages(receiver_id, status=MessageStatus.DELIVERED)
    assert len(delivered) == 1


@pytest.mark.parametrize(
    ("initial_status", "expected_status"),
    [
        (TerminalStatus.IDLE, MessageStatus.DELIVERED),
        (TerminalStatus.PROCESSING, MessageStatus.PENDING),
    ],
)
def test_api_inbox_endpoint_immediate_delivery_attempt(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
    initial_status,
    expected_status,
):
    create_resp = api_client.post(
        "/sessions",
        params={"provider": "q_cli", "agent_profile": "reviewer", "session_name": "cao-api-inbox"},
    )
    assert create_resp.status_code == 201
    receiver_id = create_resp.json()["id"]

    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [initial_status]

    send_resp = api_client.post(
        f"/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": "sender-x", "message": "hello from api"},
    )
    assert send_resp.status_code == 201

    messages = database.get_inbox_messages(receiver_id, limit=5)
    assert len(messages) == 1
    assert messages[0].status == expected_status


def test_api_inbox_endpoint_parallel_idempotent_creates_single_message(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    api_client,
):
    create_resp = api_client.post(
        "/sessions",
        params={"provider": "q_cli", "agent_profile": "reviewer", "session_name": "cao-api-race"},
    )
    assert create_resp.status_code == 201
    receiver_id = create_resp.json()["id"]

    provider = fake_provider_manager.get_provider(receiver_id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.PROCESSING]

    def _send() -> int:
        response = api_client.post(
            f"/terminals/{receiver_id}/inbox/messages",
            params={
                "sender_id": "sender-x",
                "message": "hello from api race",
                "idempotency_key": "api-race-key",
            },
        )
        assert response.status_code == 201
        return int(response.json()["message_id"])

    with ThreadPoolExecutor(max_workers=6) as executor:
        message_ids = list(executor.map(lambda _x: _send(), range(12)))

    assert len(set(message_ids)) == 1
    rows = database.get_inbox_messages(receiver_id, limit=20)
    matching = [row for row in rows if row.idempotency_key == "api-race-key"]
    assert len(matching) == 1
