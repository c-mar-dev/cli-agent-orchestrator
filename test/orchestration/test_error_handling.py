"""Error-path orchestration tests with side-effect assertions."""

from __future__ import annotations

import asyncio

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, session_service, terminal_service
from test.conftest import FakeProvider


class _Response:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_handoff_terminal_creation_failure_returns_none_and_no_orphans(monkeypatch, fake_tmux):
    monkeypatch.setattr(
        server,
        "_create_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("create failed")),
    )

    result = asyncio.run(server._handoff_impl("developer", "work", timeout=5))

    assert result.success is False
    assert result.terminal_id is None
    assert fake_tmux._sessions == {}


def test_handoff_exception_after_creation_returns_none(monkeypatch, patch_async_sleep):
    monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("deadbeef", "q_cli"))
    monkeypatch.setattr(server, "wait_until_terminal_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "_send_direct_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("output failed")),
    )

    result = asyncio.run(server._handoff_impl("developer", "work", timeout=5))

    assert result.success is False
    assert result.terminal_id is None


def test_handoff_timeout_does_not_send_exit(monkeypatch, patch_async_sleep):
    monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("deadbeef", "q_cli"))

    call_count = {"n": 0}

    def _wait(*_args, **_kwargs):
        call_count["n"] += 1
        return call_count["n"] == 1

    monkeypatch.setattr(server, "wait_until_terminal_status", _wait)
    monkeypatch.setattr(server, "_send_direct_input", lambda *_args, **_kwargs: None)

    posted = []
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda url, params=None: posted.append((url, params)) or _Response({"success": True}),
    )

    result = asyncio.run(server._handoff_impl("developer", "work", timeout=2))

    assert result.success is False
    assert all(not url.endswith("/exit") for (url, _params) in posted)


def test_inbox_dead_provider_raises_and_message_stays_pending(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    receiver = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="dead-provider",
        new_session=True,
    )
    database.create_inbox_message("sender", receiver.id, "hello")

    monkeypatch.setattr(fake_provider_manager, "get_provider", lambda _tid: None)

    with pytest.raises(ValueError, match="Provider not found"):
        inbox_service.check_and_send_pending_messages(receiver.id)

    pending = database.get_inbox_messages(receiver.id, status=MessageStatus.PENDING)
    assert len(pending) == 1


def test_inbox_send_keys_failure_transitions_to_retrying(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    receiver = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="send-fail",
        new_session=True,
    )

    provider = fake_provider_manager.get_provider(receiver.id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.IDLE]

    database.create_inbox_message("sender", receiver.id, "boom")
    fake_tmux._raise_on_send_keys = True

    delivered = inbox_service.check_and_send_pending_messages(receiver.id)
    assert delivered is False

    retrying = database.get_inbox_messages(receiver.id, status=MessageStatus.RETRYING)
    pending = database.get_inbox_messages(receiver.id, status=MessageStatus.PENDING)
    assert len(retrying) == 1
    assert pending == []


def test_delete_session_kill_false_still_cleans_db_and_providers(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    t1 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="kill-false",
        new_session=True,
    )
    t2 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name="cao-kill-false",
        new_session=False,
    )

    monkeypatch.setattr(fake_tmux, "kill_session", lambda _name: False)

    assert session_service.delete_session("cao-kill-false") is True
    assert database.list_terminals_by_session("cao-kill-false") == []
    assert t1.id not in fake_provider_manager._providers
    assert t2.id not in fake_provider_manager._providers
