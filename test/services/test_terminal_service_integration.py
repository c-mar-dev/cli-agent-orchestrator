"""Integration tests for terminal service CRUD and behavior."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.approval import ApprovalStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import OutputMode
from test.conftest import FakeProvider


def test_create_terminal_new_session_persists_metadata(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="main",
        new_session=True,
    )

    assert terminal.id
    assert terminal.session_name == "cao-main"
    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None
    assert metadata["provider"] == "q_cli"


def test_create_terminal_applies_cao_prefix(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="no-prefix",
        new_session=True,
    )

    assert terminal.session_name.startswith("cao-")


def test_create_terminal_passes_working_directory(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="cwd",
        new_session=True,
        working_directory="/tmp/project",
    )

    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None
    assert metadata["launch_cwd"] == "/tmp/project"


def test_get_terminal_reads_live_status_from_provider(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="status",
        new_session=True,
    )

    provider = fake_provider_manager.get_provider(terminal.id)
    assert isinstance(provider, FakeProvider)
    provider.status_sequence = [TerminalStatus.PROCESSING]

    payload = terminal_service.get_terminal(terminal.id)
    assert payload["status"] == TerminalStatus.PROCESSING.value


def test_send_input_records_keys_and_updates_last_active(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="send",
        new_session=True,
    )

    before = database.get_terminal_metadata(terminal.id)
    assert before is not None
    old_last_active = before["last_active"]

    assert terminal_service.send_input(terminal.id, "hello") is True

    after = database.get_terminal_metadata(terminal.id)
    assert after is not None
    assert after["last_active"] >= old_last_active
    assert any(keys == "hello" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_get_output_full_mode_returns_history(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="full-output",
        new_session=True,
    )

    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None
    fake_tmux.set_pane_content(metadata["tmux_session"], metadata["tmux_window"], content="line-1\nline-2")

    output = terminal_service.get_output(terminal.id, OutputMode.FULL)
    assert output == "line-1\nline-2"


def test_get_output_last_mode_uses_provider_extractor(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="last-output",
        new_session=True,
    )

    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None
    fake_tmux.set_pane_content(metadata["tmux_session"], metadata["tmux_window"], content="raw content")

    provider = fake_provider_manager.get_provider(terminal.id)
    assert isinstance(provider, FakeProvider)
    provider._last_message = "parsed message"

    output = terminal_service.get_output(terminal.id, OutputMode.LAST)
    assert output == "parsed message"


def test_delete_terminal_cleans_provider_db_and_pipe(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="delete-terminal",
        new_session=True,
    )

    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None

    assert terminal_service.delete_terminal(terminal.id) is True
    assert database.get_terminal_metadata(terminal.id) is None
    assert terminal.id not in fake_provider_manager._providers
    assert (metadata["tmux_session"], metadata["tmux_window"]) in fake_tmux._stopped_pipes


def test_create_terminal_rolls_back_session_on_provider_init_failure(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    class _BrokenProvider(FakeProvider):
        def initialize(self) -> bool:
            raise RuntimeError("provider init failed")

    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda _profile: "broken-win")

    def _create_broken(
        _provider_type: str,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: str | None = None,
    ):
        provider = _BrokenProvider(terminal_id, tmux_session, tmux_window)
        fake_provider_manager._providers[terminal_id] = provider
        return provider

    monkeypatch.setattr(fake_provider_manager, "create_provider", _create_broken)

    with pytest.raises(RuntimeError, match="provider init failed"):
        terminal_service.create_terminal(
            provider="q_cli",
            agent_profile="developer",
            session_name="broken-session",
            new_session=True,
        )

    assert fake_tmux.session_exists("cao-broken-session") is False


def test_delete_terminal_resolves_pending_approvals(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="delete-approvals",
        new_session=True,
    )
    approval, _is_new = database.create_or_get_pending_approval(
        terminal.id,
        provider="q_cli",
        status_reason_code="waiting",
        prompt_excerpt="approve?",
        source="status",
    )
    assert approval.status == ApprovalStatus.PENDING

    assert terminal_service.delete_terminal(terminal.id) is True

    pending = database.list_approval_requests(terminal.id, status=ApprovalStatus.PENDING)
    assert pending == []
    resolved = database.list_approval_requests(terminal.id, status=ApprovalStatus.RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].resolution_message == "terminal-deleted"


def test_delete_terminal_attempts_approval_cleanup_when_provider_cleanup_fails(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="delete-partial-failure",
        new_session=True,
    )
    database.create_or_get_pending_approval(
        terminal.id,
        provider="q_cli",
        status_reason_code="waiting",
        prompt_excerpt="approve?",
        source="status",
    )

    def _boom(_terminal_id: str) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(fake_provider_manager, "cleanup_provider", _boom)

    assert terminal_service.delete_terminal(terminal.id) is True
    resolved = database.list_approval_requests(terminal.id, status=ApprovalStatus.RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].resolution_message == "terminal-deleted"
